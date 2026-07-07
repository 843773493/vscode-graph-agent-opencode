from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from urllib.request import Request
from urllib.request import urlopen

import httpx
import pytest
import websockets

from app.agents.agent_tools import create_persistent_terminal_tool
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from tests.e2e.terminal.terminal_process_helpers import (
    kill_process_on_port,
    seed_historical_terminal_checkpoint,
    terminal_ports,
    terminate_process,
    wait_for_http_ok,
)


async def _recv_ws_type(websocket, expected_type: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(websocket.recv(), timeout=max(deadline - time.monotonic(), 0.1))
        message = json.loads(raw)
        if message.get("type") == expected_type:
            return message
    raise TimeoutError(f"未收到 WebSocket 消息: {expected_type}")


@pytest.fixture(scope="module", autouse=True)
def terminal_manager_env(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> Generator[tuple[int, int], None, None]:
    backend_port, frontend_port = terminal_ports(e2e_backend_port)
    workspace_root = str(Path(e2e_workspace_root_path).resolve())
    updates = {
        "WORKSPACE_ROOT": workspace_root,
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
    terminal_manager_env: tuple[int, int],
    e2e_workspace_root_path: str,
) -> Generator[tuple[int, int], None, None]:
    backend_port, frontend_port = terminal_manager_env
    kill_process_on_port(backend_port)
    kill_process_on_port(frontend_port)

    project_root = Path.cwd().resolve()
    workspace_root = str(Path(e2e_workspace_root_path).resolve())
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动终端 e2e 进程")

    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = workspace_root
    env["BOXTEAM_TERMINAL_WORKSPACE_ROOT"] = workspace_root

    backend_process = subprocess.Popen(
        [
            node_bin,
            "backend.js",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
            "--frontend-url",
            f"http://127.0.0.1:{frontend_port}",
            "--workspace-root",
            workspace_root,
        ],
        cwd=project_root / "src" / "terminal" / "server",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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
        cwd=project_root / "src" / "terminal" / "client",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        wait_for_http_ok(f"http://127.0.0.1:{backend_port}/health", backend_process)
        wait_for_http_ok(f"http://127.0.0.1:{frontend_port}/health", frontend_process)
        yield backend_port, frontend_port
    finally:
        terminate_process(frontend_process)
        terminate_process(backend_process)
        kill_process_on_port(frontend_port)
        kill_process_on_port(backend_port)


@pytest.mark.asyncio
async def test_persistent_terminal_tool_is_visible_and_resource_can_be_attached(
    terminal_manager_processes: tuple[int, int],
    client: httpx.AsyncClient,
):
    backend_port, frontend_port = terminal_manager_processes
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Persistent Terminal Resource Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]

    tools_response = await client.get("/api/v1/tools")
    assert tools_response.status_code == 200
    tool_ids = {tool["tool_id"] for tool in tools_response.json()["data"]}
    assert "persistent_terminal" in tool_ids

    tool = create_persistent_terminal_tool(
        session_id=session_id,
        agent_id="default",
        terminal_client=TerminalManagerClient(),
    )
    result = await tool.ainvoke(
        {
            "action": "run_command",
            "command": "printf 'BOXTEAM_TERMINAL_OK\\n'",
            "timeout_seconds": 5,
        }
    )
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert "BOXTEAM_TERMINAL_OK" in result["output"]
    terminal_id = result["terminal_id"]

    reuse_result = await tool.ainvoke(
        {
            "action": "run_command",
            "command": "printf 'BOXTEAM_TERMINAL_REUSE\\n'",
            "timeout_seconds": 5,
        }
    )
    assert reuse_result["status"] == "completed"
    assert reuse_result["terminal_id"] == terminal_id
    assert "BOXTEAM_TERMINAL_REUSE" in reuse_result["output"]

    with urlopen(f"http://127.0.0.1:{backend_port}/api/terminals/{terminal_id}", timeout=5) as response:
        completed_snapshot = json.loads(response.read().decode("utf-8"))["data"]
    command_completed_at = completed_snapshot["last_command_completed_at"]
    write_request = Request(
        f"http://127.0.0.1:{backend_port}/api/terminals/{terminal_id}/write",
        data=json.dumps({"data": "echo BOXTEAM_USER_AFTER_COMPLETE\r", "source": "user"}).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urlopen(write_request, timeout=5) as response:
        user_write_snapshot = json.loads(response.read().decode("utf-8"))["data"]
    assert user_write_snapshot["last_command"] == "printf 'BOXTEAM_TERMINAL_REUSE\\n'"
    assert user_write_snapshot["last_command_completed_at"] == command_completed_at
    assert user_write_snapshot["last_input"] == "echo BOXTEAM_USER_AFTER_COMPLETE"

    background_result = await tool.ainvoke(
        {
            "action": "run_command",
            "terminal_id": terminal_id,
            "command": "printf 'BOXTEAM_BACKGROUND_START\\n'; sleep 20",
            "timeout_seconds": 1,
        }
    )
    assert background_result["status"] == "background"
    assert background_result["terminal_id"] == terminal_id
    assert "BOXTEAM_BACKGROUND_START" in background_result["recent_output"]

    resources_response = await client.get(f"/api/v1/sessions/{session_id}/resources")
    assert resources_response.status_code == 200
    resources = resources_response.json()["data"]["items"]
    terminal_resources = [
        resource
        for resource in resources
        if resource["kind"] == "terminal"
    ]
    assert len(terminal_resources) == 1
    terminal_resource = next(
        resource
        for resource in resources
        if resource["kind"] == "terminal" and resource["resource_id"] == terminal_id
    )
    assert terminal_resource["status"] == "running"
    assert "cancel" in terminal_resource["available_actions"]
    assert "delete" in terminal_resource["available_actions"]
    assert terminal_resource["metadata"]["attach_url"] == (
        f"http://127.0.0.1:{frontend_port}/?terminalId={terminal_id}"
    )
    assert terminal_resource["metadata"]["command_status"] == "running"
    assert terminal_resource["metadata"]["command_exit_code"] is None

    async with websockets.connect(f"ws://127.0.0.1:{backend_port}/terminal") as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "attach",
                    "terminalId": terminal_id,
                    "cols": 100,
                    "rows": 30,
                }
            )
        )
        attached_message = await _recv_ws_type(websocket, "attached")
        assert attached_message["type"] == "attached"
        assert attached_message["snapshot"]["client_count"] == 1

        attached_resources_response = await client.get(f"/api/v1/sessions/{session_id}/resources")
        assert attached_resources_response.status_code == 200
        attached_terminal = next(
            resource
            for resource in attached_resources_response.json()["data"]["items"]
            if resource["kind"] == "terminal" and resource["resource_id"] == terminal_id
        )
        assert attached_terminal["metadata"]["client_count"] == 1

        await websocket.send(json.dumps({"type": "input", "data": "echo BOXTEAM_WS_INPUT\r"}))
        await _recv_ws_type(websocket, "input")
        input_resources_response = await client.get(f"/api/v1/sessions/{session_id}/resources")
        assert input_resources_response.status_code == 200, (
            f"资源列表请求失败: url={input_resources_response.url}, "
            f"status={input_resources_response.status_code}, body={input_resources_response.text}"
        )
        input_terminal = next(
            resource
            for resource in input_resources_response.json()["data"]["items"]
            if resource["kind"] == "terminal" and resource["resource_id"] == terminal_id
        )
        assert input_terminal["metadata"]["last_input"] == "echo BOXTEAM_WS_INPUT"
        assert input_terminal["metadata"]["last_input_source"] == "user"

        await websocket.send(json.dumps({"type": "detach"}))
        detached_message = await _recv_ws_type(websocket, "detached")
        assert detached_message["type"] == "detached"

        detached_resources_response = await client.get(f"/api/v1/sessions/{session_id}/resources")
        assert detached_resources_response.status_code == 200
        detached_terminal = next(
            resource
            for resource in detached_resources_response.json()["data"]["items"]
            if resource["kind"] == "terminal" and resource["resource_id"] == terminal_id
        )
        assert detached_terminal["metadata"]["client_count"] == 0

    with urlopen(terminal_resource["metadata"]["attach_url"], timeout=5) as response:
        html = response.read().decode("utf-8")
    assert response.status == 200
    assert "持久终端" in html

    with urlopen(f"http://127.0.0.1:{backend_port}/api/terminals/{terminal_id}", timeout=5) as response:
        snapshot_body = response.read().decode("utf-8")
    snapshot = json.loads(snapshot_body)["data"]
    assert "BOXTEAM_TERMINAL_OK" in snapshot["display_buffer"]
    assert "__BOXTEAM_CMD_START_" not in snapshot["display_buffer"]
    assert "__BOXTEAM_CMD_DONE_" not in snapshot["display_buffer"]

    cancel_response = await client.post(
        f"/api/v1/sessions/{session_id}/resources/terminal/{terminal_id}/control",
        json={"action": "cancel"},
    )
    assert cancel_response.status_code == 200
    cancel_data = cancel_response.json()["data"]
    assert cancel_data["kind"] == "terminal"
    assert cancel_data["resource"]["status"] == "terminated"

    delete_response = await client.post(
        f"/api/v1/sessions/{session_id}/resources/terminal/{terminal_id}/control",
        json={"action": "delete"},
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["data"]["status"] == "deleted"

    deleted_resources_response = await client.get(f"/api/v1/sessions/{session_id}/resources")
    assert deleted_resources_response.status_code == 200
    deleted_resources = deleted_resources_response.json()["data"]["items"]
    deleted_terminal = next(
        resource
        for resource in deleted_resources
        if resource["kind"] == "terminal" and resource["resource_id"] == terminal_id
    )
    assert deleted_terminal["status"] == "deleted"
    assert deleted_terminal["available_actions"] == []
    assert "终端已删除" in deleted_terminal["metadata"]["status_note"]

    with urlopen(f"http://127.0.0.1:{backend_port}/api/terminals/{terminal_id}", timeout=5) as response:
        deleted_snapshot = json.loads(response.read().decode("utf-8"))["data"]
    assert deleted_snapshot["status"] == "deleted"


@pytest.mark.asyncio
async def test_session_resources_restore_missing_terminal_refs_from_agent_state(
    terminal_manager_processes: tuple[int, int],
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
):
    backend_port, frontend_port = terminal_manager_processes
    create_session_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Historical Terminal Resource Test"},
    )
    assert create_session_response.status_code == 200
    session_id = create_session_response.json()["data"]["session_id"]
    terminal_id = "term_historical_missing"

    seed_historical_terminal_checkpoint(
        workspace_root=e2e_workspace_root_path,
        session_id=session_id,
        terminal_id=terminal_id,
        frontend_port=frontend_port,
    )

    resources_response = await client.get(f"/api/v1/sessions/{session_id}/resources")
    assert resources_response.status_code == 200
    resources = resources_response.json()["data"]["items"]
    historical_terminal = next(
        resource
        for resource in resources
        if resource["kind"] == "terminal" and resource["resource_id"] == terminal_id
    )
    assert historical_terminal["status"] == "deleted"
    assert historical_terminal["available_actions"] == []
    assert historical_terminal["metadata"]["resource_source"] == "历史记录"
    assert "终端管理器中已无该终端" in historical_terminal["metadata"]["status_note"]
    assert historical_terminal["metadata"]["historical_status"] == "running"
    assert historical_terminal["metadata"]["command_status"] == "deleted"
    assert historical_terminal["metadata"]["attach_url"] == (
        f"http://127.0.0.1:{frontend_port}/?terminalId={terminal_id}"
    )

    with urlopen(
        f"http://127.0.0.1:{backend_port}/api/terminals/{terminal_id}?missing_as_deleted=1",
        timeout=5,
    ) as response:
        missing_snapshot = json.loads(response.read().decode("utf-8"))["data"]
    assert response.status == 200
    assert missing_snapshot["status"] == "deleted"
