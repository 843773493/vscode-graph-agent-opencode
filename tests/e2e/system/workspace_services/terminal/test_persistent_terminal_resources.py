from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
import websockets

from app.core.workspace_identity import load_or_create_workspace_id
from tests.e2e.system.workspace_services.terminal.terminal_process_helpers import (
    seed_historical_terminal_checkpoint,
    start_terminal_backend,
    terminal_ports,
    wait_for_http_ok,
)
from tests.support.processes import kill_process_on_port, terminate_process
from tests.support.session_navigation_http import submit_navigation_operation


async def _recv_ws_type(websocket, expected_type: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(
            websocket.recv(), timeout=max(deadline - time.monotonic(), 0.1)
        )
        message = json.loads(raw)
        if message.get("type") == "error":
            raise AssertionError(f"Terminal WS 拒绝操作: {message}")
        if message.get("type") == expected_type:
            return message
    raise TimeoutError(f"未收到 WebSocket 消息: {expected_type}")


@pytest.fixture(scope="module", autouse=True)
def terminal_manager_env(
    e2e_workspace_root_path: str, e2e_backend_port: int
) -> Generator[tuple[int, int], None, None]:
    backend_port, frontend_port = terminal_ports(e2e_backend_port)
    workspace_root = str(Path(e2e_workspace_root_path).resolve())
    updates = {
        "WORKSPACE_ROOT": workspace_root,
        "BOXTEAM_WORKSPACE_ID": load_or_create_workspace_id(Path(workspace_root)),
        "BOXTEAM_TERMINAL_BACKEND_URL": f"http://127.0.0.1:{backend_port}",
        "BOXTEAM_TERMINAL_FRONTEND_URL": f"http://127.0.0.1:{frontend_port}",
    }
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield backend_port, frontend_port
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def terminal_manager_processes(
    terminal_manager_env: tuple[int, int], e2e_workspace_root_path: str
) -> Generator[tuple[int, int], None, None]:
    backend_port, frontend_port = terminal_manager_env
    kill_process_on_port(backend_port)
    kill_process_on_port(frontend_port)
    project_root = Path.cwd().resolve()
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动终端 e2e 进程")
    backend_process = start_terminal_backend(
        backend_port=backend_port, workspace_root=e2e_workspace_root_path
    )
    frontend_process = subprocess.Popen(
        [
            node_bin,
            "server.js",
            "--host",
            "127.0.0.1",
            "--port",
            str(frontend_port),
            "--backend-url",
            f"http://127.0.0.1:{backend_port}",
        ],
        cwd=project_root / "src/workspace-services/terminal/client",
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_http_ok(f"http://127.0.0.1:{frontend_port}/health", frontend_process)
        yield backend_port, frontend_port
    finally:
        terminate_process(frontend_process)
        terminate_process(backend_process)
        kill_process_on_port(frontend_port)
        kill_process_on_port(backend_port)


async def _create_resource(
    client: httpx.AsyncClient, title: str, *, scope: str = "thread"
) -> dict[str, object]:
    response = await client.post("/api/v1/sessions", json={"title": title})
    response.raise_for_status()
    session = response.json()["data"]
    main_response = await client.get(
        f"/api/v1/sessions/{session['session_id']}/main-thread"
    )
    main_response.raise_for_status()
    thread_id = main_response.json()["data"]["thread_id"]
    response = await client.post(
        "/api/v1/resources/terminals",
        json={
            "session_id": session["session_id"],
            "thread_id": thread_id,
            "retention_scope": scope,
            "title": title,
            "cwd_relative": ".",
        },
    )
    response.raise_for_status()
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    resource = response.json()["data"]
    assert resource["source_thread"] == {
        "session_id": session["session_id"],
        "thread_id": thread_id,
    }
    assert resource["thread_associations"] == [resource["source_thread"]]
    return resource


async def _grant(client: httpx.AsyncClient, resource: dict[str, object]) -> str:
    response = await client.post(
        f"/api/v1/resources/{resource['resource_id']}/transport-grant",
        json={
            "expected_revision": resource["owner_revision"],
            "thread": resource["source_thread"],
        },
    )
    response.raise_for_status()
    return response.json()["data"]["grant_token"]


@pytest.mark.asyncio
async def test_workspace_owner_resource_grant_attach_and_exact_detach_delete(
    terminal_manager_processes: tuple[int, int],
    client: httpx.AsyncClient,
) -> None:
    backend_port, frontend_port = terminal_manager_processes
    resource = await _create_resource(client, "Persistent Terminal Resource Test")
    terminal_id = resource["resource_id"]
    source = resource["source_thread"]
    tools_response = await client.get("/api/v1/tools")
    tools_response.raise_for_status()
    assert {
        "exec_command",
        "write_stdin",
        "list_terminal_sessions",
        "kill_terminal",
    }.issubset({tool["tool_id"] for tool in tools_response.json()["data"]})
    resources_response = await client.get(
        f"/api/v1/sessions/{source['session_id']}/resources"
    )
    resources_response.raise_for_status()
    terminal = next(
        item
        for item in resources_response.json()["data"]["items"]
        if item["resource_id"] == terminal_id
    )
    assert terminal["status"] == "running"
    assert terminal["available_actions"] == ["cancel"]
    assert terminal["metadata"]["thread_associations"] == [source]
    assert "attach_url" not in terminal["metadata"]

    grant = await _grant(client, resource)
    async with websockets.connect(
        f"ws://127.0.0.1:{backend_port}/terminal?workspace_id={os.environ['BOXTEAM_WORKSPACE_ID']}"
    ) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "attach",
                    "terminalId": terminal_id,
                    "grantToken": grant,
                    "cols": 100,
                    "rows": 30,
                }
            )
        )
        attached = await _recv_ws_type(websocket, "attached")
        assert attached["snapshot"]["client_count"] == 1
        assert not {
            "session_id",
            "source_thread_id",
            "retention_scope",
            "thread_associations",
            "owner_revision",
        }.intersection(attached["snapshot"])
        await websocket.send(
            json.dumps({"type": "input", "data": "printf 'BOXTEAM_TERMINAL_OK\\n'\r"})
        )
        await _recv_ws_type(websocket, "input")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            output = (await client.get(f"/api/v1/resources/{terminal_id}")).json()[
                "data"
            ]
            if "BOXTEAM_TERMINAL_OK" in output["metadata"]["display_buffer"]:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("真实 PTY 输出未进入 owner 投影")
        assert output["metadata"]["client_count"] == 1
        assert output["metadata"]["last_input_source"] == "user"
        await websocket.send(json.dumps({"type": "detach"}))
        await _recv_ws_type(websocket, "detached")

    frontend = await client.get(
        f"http://127.0.0.1:{frontend_port}/?terminalId={terminal_id}"
    )
    frontend.raise_for_status()
    assert "持久终端" in frontend.text
    promoted = await client.post(
        f"/api/v1/resources/{terminal_id}/promote",
        json={"expected_revision": resource["owner_revision"]},
    )
    promoted.raise_for_status()
    resource = promoted.json()["data"]
    assert resource["source_thread"] == source
    assert resource["retention_scope"] == "workspace"
    cancel = await client.post(
        f"/api/v1/sessions/{source['session_id']}/resources/terminal/{terminal_id}/control",
        json={"action": "cancel"},
    )
    cancel.raise_for_status()
    assert cancel.json()["data"]["resource"]["status"] == "terminated"
    resource = (await client.get(f"/api/v1/resources/{terminal_id}")).json()["data"]
    deletion = await client.post(
        f"/api/v1/resources/{terminal_id}/delete",
        json={"expected_revision": resource["owner_revision"]},
    )
    assert deletion.status_code == 409
    session_delete = await client.post(
        f"/api/v1/sessions/{source['session_id']}/resources/terminal/{terminal_id}/control",
        json={"action": "delete"},
    )
    assert session_delete.status_code == 409
    detach = await client.post(
        f"/api/v1/resources/{terminal_id}/detach",
        json={"expected_revision": resource["owner_revision"], "thread": source},
    )
    detach.raise_for_status()
    detached = detach.json()["data"]
    assert detached["thread_associations"] == []
    assert detached["unattached_since"] is None and detached["reclaim_at"] is None
    deleted = await client.post(
        f"/api/v1/resources/{terminal_id}/delete",
        json={"expected_revision": detached["owner_revision"]},
    )
    deleted.raise_for_status()
    assert deleted.json()["data"]["status"] == "deleted"
    assert (await client.get(f"/api/v1/resources/{terminal_id}")).json()["data"][
        "status"
    ] == "deleted"
    assert all(
        item["resource_id"] != terminal_id
        for item in (
            await client.get(f"/api/v1/sessions/{source['session_id']}/resources")
        ).json()["data"]["items"]
    )


@pytest.mark.asyncio
async def test_terminal_resize_does_not_create_fake_newlines_or_input(
    terminal_manager_processes: tuple[int, int],
    client: httpx.AsyncClient,
) -> None:
    backend_port, _frontend_port = terminal_manager_processes
    resource = await _create_resource(client, "Terminal Resize Regression")
    terminal_id = resource["resource_id"]
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{backend_port}",
        headers={
            "X-BoxTeam-Workspace-Id": os.environ["BOXTEAM_WORKSPACE_ID"],
            "X-BoxTeam-Resource-Owner": "workspace-terminal",
        },
        timeout=5,
    ) as transport:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            candidate = (await transport.get(f"/api/terminals/{terminal_id}")).json()[
                "data"
            ]
            if str(candidate["buffer"]).endswith("$ "):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("交互式 shell 未在期限内进入 prompt")
        grant = await _grant(client, resource)
        async with websockets.connect(
            f"ws://127.0.0.1:{backend_port}/terminal?workspace_id={os.environ['BOXTEAM_WORKSPACE_ID']}"
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "attach",
                        "terminalId": terminal_id,
                        "grantToken": grant,
                        "cols": 80,
                        "rows": 24,
                    }
                )
            )
            await _recv_ws_type(websocket, "attached")
            await asyncio.sleep(0.1)
            before = (await transport.get(f"/api/terminals/{terminal_id}")).json()[
                "data"
            ]
            for _ in range(12):
                await websocket.send(
                    json.dumps({"type": "resize", "cols": 80, "rows": 24})
                )
            await asyncio.sleep(0.15)
            unchanged = (await transport.get(f"/api/terminals/{terminal_id}")).json()[
                "data"
            ]
            assert unchanged["sequence"] == before["sequence"]
            assert (
                unchanged["last_input"] is None
                and unchanged["last_input_source"] is None
            )
            for cols in (79, 78, 77, 76):
                await websocket.send(
                    json.dumps({"type": "resize", "cols": cols, "rows": 24})
                )
            await asyncio.sleep(0.2)
        resized = (await transport.get(f"/api/terminals/{terminal_id}")).json()["data"]
        assert resized["last_input"] is None and resized["last_input_source"] is None
        assert "\r\x1b[K\r" in resized["display_buffer"]
        assert "\r\n\x1b[K\r\n" not in resized["display_buffer"]
    detach = await client.post(
        f"/api/v1/resources/{terminal_id}/detach",
        json={
            "expected_revision": resource["owner_revision"],
            "thread": resource["source_thread"],
        },
    )
    detach.raise_for_status()
    detached = detach.json()["data"]
    assert detached["status"] == "running"
    assert detached["unattached_since"] and detached["reclaim_at"]
    deleted = await client.post(
        f"/api/v1/resources/{terminal_id}/delete",
        json={"expected_revision": detached["owner_revision"]},
    )
    deleted.raise_for_status()


@pytest.mark.asyncio
async def test_historical_terminal_checkpoint_does_not_create_resource_owner(
    terminal_manager_processes: tuple[int, int],
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    del terminal_manager_processes
    response = await client.post(
        "/api/v1/sessions", json={"title": "Historical Terminal Resource Test"}
    )
    response.raise_for_status()
    session_id = response.json()["data"]["session_id"]
    terminal_id = "term_historical_missing"
    seed_historical_terminal_checkpoint(
        workspace_root=e2e_workspace_root_path,
        session_id=session_id,
        terminal_id=terminal_id,
    )
    resources = await client.get(f"/api/v1/sessions/{session_id}/resources")
    resources.raise_for_status()
    assert all(
        item["resource_id"] != terminal_id for item in resources.json()["data"]["items"]
    )
    assert (await client.get(f"/api/v1/resources/{terminal_id}")).status_code == 404


@pytest.mark.asyncio
async def test_workspace_terminal_survives_source_session_delete(
    terminal_manager_processes: tuple[int, int],
    client: httpx.AsyncClient,
) -> None:
    del terminal_manager_processes
    resource = await _create_resource(
        client, "Workspace terminal survives source", scope="workspace"
    )
    terminal_id = resource["resource_id"]
    client.headers.update(
        {
            "X-BoxTeam-Gateway-Id": "terminal-e2e",
            "X-BoxTeam-Navigation-Actor": "guest:terminal-e2e",
        }
    )
    receipt = await submit_navigation_operation(
        client, "delete_session", target_node_id=resource["source_thread"]["session_id"]
    )
    assert receipt["state"] == "committed", receipt
    survivor = (await client.get(f"/api/v1/resources/{terminal_id}")).json()["data"]
    assert survivor["source_thread"] == resource["source_thread"]
    assert survivor["thread_associations"] == [] and survivor["status"] == "running"
    assert survivor["unattached_since"] is None and survivor["reclaim_at"] is None
    deleted = await client.post(
        f"/api/v1/resources/{terminal_id}/delete",
        json={"expected_revision": survivor["owner_revision"]},
    )
    deleted.raise_for_status()
