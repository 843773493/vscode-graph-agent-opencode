from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import httpx
import pytest
import websockets

from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.runtime.local_workspace import (
    _adopt_terminal_manager,
    start_managed_local_workspace_runtime,
)
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file


@pytest.mark.asyncio
async def test_managed_terminal_uses_backend_uuid_and_python_client_controls_it(
    integration_workspace_root_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_executable = shutil.which("node")
    if node_executable is None:
        raise RuntimeError("托管 Terminal 集成验收需要 PATH 中存在 node")
    monkeypatch.setenv("BOXTEAM_NODE_BIN", node_executable)
    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", sys.executable)

    workspace_root = Path(integration_workspace_root_path).resolve()
    artifacts = workspace_root.parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    backend_workspace_id = load_or_create_workspace_id(workspace_root)
    runtime = await start_managed_local_workspace_runtime(
        project_root=Path.cwd().resolve(),
        workspace_root=workspace_root,
        expected_backend_workspace_id=backend_workspace_id,
        log_dir=artifacts / "managed-runtime-logs",
    )
    try:
        assert runtime.backend_workspace_id == backend_workspace_id
        terminal_url = runtime.service_urls["terminal_manager"]
        async with httpx.AsyncClient(timeout=3) as client:
            health_response = await client.get(f"{terminal_url}/health")
        health_response.raise_for_status()
        terminal_health = health_response.json()
        assert terminal_health["workspace_id"] == backend_workspace_id
        assert "workspace_root" not in terminal_health
        assert isinstance(terminal_health["process_id"], int)

        with pytest.raises(RuntimeError, match="backend UUID 不匹配"):
            await _adopt_terminal_manager(
                service_url=terminal_url,
                backend_workspace_id="00000000-0000-4000-8000-000000000002",
            )

        adopted = await _adopt_terminal_manager(
            service_url=terminal_url,
            backend_workspace_id=backend_workspace_id,
        )
        assert adopted is not None
        assert adopted.pid == terminal_health["process_id"]
        assert runtime.processes["terminal_manager"].is_alive()

        async with httpx.AsyncClient(timeout=3) as client:
            still_running = await client.get(f"{terminal_url}/health")
        still_running.raise_for_status()
        assert still_running.json()["process_id"] == terminal_health["process_id"]

        terminal_client = TerminalManagerClient(
            backend_url=terminal_url,
            workspace_id=backend_workspace_id,
        )
        workspace_api_path = f"/api/v1/workspaces/{backend_workspace_id}"
        async with httpx.AsyncClient(
            base_url=runtime.service_urls["workspace_api"],
            headers=LOCAL_TOKEN_HEADERS,
            timeout=3,
        ) as client:
            session_response = await client.post(
                f"{workspace_api_path}/sessions",
                json={"title": "Managed Terminal owner check"},
            )
            session_response.raise_for_status()
            session_id = session_response.json()["data"]["session_id"]
            thread_response = await client.get(
                f"{workspace_api_path}/sessions/{session_id}/main-thread"
            )
            thread_response.raise_for_status()
            thread_id = thread_response.json()["data"]["thread_id"]
            created_response = await client.post(
                f"{workspace_api_path}/resources/terminals",
                json={
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "retention_scope": "thread",
                    "title": "Managed Terminal owner check",
                    "cwd_relative": ".",
                },
            )
            created_response.raise_for_status()
            created = created_response.json()["data"]
            terminal_id = created["resource_id"]
            assert created["workspace_id"] == backend_workspace_id
            assert created["source_thread"] == {
                "session_id": session_id,
                "thread_id": thread_id,
            }
            assert created["thread_associations"] == [created["source_thread"]]
            assert (
                created_response.headers["X-Request-ID"]
                == created_response.json()["request_id"]
            )
            # 第二种寻址载体也必须命中同一个 owner 资源，不能产生另一份事实。
            explicit = await client.get(
                f"/api/v1/resources/{terminal_id}",
                headers={"X-BoxTeam-Workspace-Id": backend_workspace_id},
            )
            explicit.raise_for_status()
            assert explicit.json()["data"]["resource_id"] == terminal_id
            stopped = await client.post(
                f"{workspace_api_path}/resources/{terminal_id}/stop",
                json={"expected_revision": created["owner_revision"]},
            )
            stopped.raise_for_status()
            assert stopped.json()["data"]["status"] == "terminated"

        listed = await terminal_client.list_resources()
        assert [terminal["terminal_id"] for terminal in listed] == [terminal_id]
        assert not {
            "session_id",
            "source_thread_id",
            "retention_scope",
            "thread_associations",
        }.intersection(listed[0])
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_gateway_owner_create_grant_ws_and_management_bypass_rejection(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
) -> None:
    root = Path(integration_workspace_root_path).resolve()
    port = integration_port_block_for_file(Path(request.node.path)).port(20)
    gateway = start_gateway_process(
        workspace_root=root, default_backend_url=None, port=port
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            await acquire_gateway_guest(client)
            for path in (
                "/api/gateway/health",
                "/api/gateway/workspaces",
                "/api/v1/workspace",
            ):
                response = await client.get(path)
                response.raise_for_status()
                assert response.headers["X-Request-ID"] == response.json()["request_id"]
            workspaces = (await client.get("/api/gateway/workspaces")).json()["data"]
            workspace_id = workspaces["active_workspace_id"]
            client.headers["X-BoxTeam-Workspace-Id"] = workspace_id
            session_response = await client.post(
                "/api/v1/sessions", json={"title": "Gateway Terminal"}
            )
            session_response.raise_for_status()
            session_id = session_response.json()["data"]["session_id"]
            main = await client.get(f"/api/v1/sessions/{session_id}/main-thread")
            main.raise_for_status()
            thread_id = main.json()["data"]["thread_id"]
            created = await client.post(
                "/api/v1/resources/terminals",
                headers={"X-Request-ID": "req-terminal-proxy"},
                json={
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "cwd_relative": ".",
                },
            )
            created.raise_for_status()
            assert (
                created.headers["X-Request-ID"]
                == created.json()["request_id"]
                == "req-terminal-proxy"
            )
            resource = created.json()["data"]
            rid = resource["resource_id"]
            bypass = await client.post(
                f"/api/gateway/workspaces/{workspace_id}/terminal-manager/api/terminals",
                json={"session_id": session_id},
            )
            assert bypass.status_code == 403, bypass.text
            grant_response = await client.post(
                f"/api/v1/resources/{rid}/transport-grant",
                json={
                    "expected_revision": resource["owner_revision"],
                    "thread": resource["source_thread"],
                },
            )
            grant_response.raise_for_status()
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/api/gateway/workspaces/{workspace_id}/terminal-manager/terminal?token=local-dev-token",
            ) as socket:
                await socket.send(
                    json.dumps(
                        {
                            "type": "attach",
                            "terminalId": rid,
                            "grantToken": grant_response.json()["data"]["grant_token"],
                        }
                    )
                )
                attached = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
                assert attached["type"] == "attached", attached
                await socket.send(
                    json.dumps(
                        {"type": "input", "data": "printf 'GATEWAY_TERMINAL_OK\\n'\r"}
                    )
                )
                async with asyncio.timeout(5):
                    while True:
                        message = json.loads(await socket.recv())
                        assert message["type"] != "error", message
                        if message["type"] == "output" and "GATEWAY_TERMINAL_OK" in str(
                            message.get("data", "")
                        ):
                            break
            detached = await client.post(
                f"/api/v1/resources/{rid}/detach",
                json={
                    "expected_revision": resource["owner_revision"],
                    "thread": resource["source_thread"],
                },
            )
            detached.raise_for_status()
            assert detached.json()["data"]["unattached_since"]
            deleted = await client.post(
                f"/api/v1/resources/{rid}/delete",
                json={"expected_revision": detached.json()["data"]["owner_revision"]},
            )
            deleted.raise_for_status()
            assert deleted.json()["data"]["status"] == "deleted"
    finally:
        close_gateway_process(gateway)
