from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import commentjson
import httpx
import pytest
import websockets

from tests.e2e.gateway.browser_manager import (
    BrowserFrontendProcess,
    browser_test_data_url,
    close_browser_frontend_process,
    start_browser_frontend_process,
)
from tests.e2e.gateway.gateway_docker import (
    docker_daemon_error,
    ensure_gateway_ssh_container,
)
from tests.e2e.gateway.gateway_target import (
    GatewayTargetE2EPaths,
    GatewaySshTarget,
    build_remote_pair_command,
    install_gateway_ssh_assets_for_e2e,
    start_remote_gateway_via_ssh,
    stop_remote_backend,
)
from tests.e2e.gateway.processes import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    close_gateway_process,
    start_gateway_process,
    write_gateway_remote_gateway_config,
)
from tests.e2e.gateway.terminal_manager import (
    TerminalFrontendProcess,
    close_terminal_frontend_process,
    start_terminal_frontend_process,
)
from tests.e2e.ports import e2e_port_block_for_file


async def _receive_websocket_type(websocket, expected_type: str) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(
            websocket.recv(),
            timeout=max(deadline - asyncio.get_running_loop().time(), 0.1),
        )
        message = json.loads(raw)
        if message.get("type") == expected_type:
            return message
    raise TimeoutError(f"未收到 WebSocket 消息: {expected_type}")


async def _receive_terminal_output(websocket, expected_text: str) -> None:
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(
            websocket.recv(),
            timeout=max(deadline - asyncio.get_running_loop().time(), 0.1),
        )
        message = json.loads(raw)
        if message.get("type") == "output" and expected_text in str(message.get("data")):
            return
    raise TimeoutError(f"终端 WebSocket 未输出: {expected_text}")


def _remove_declared_remote_gateway(workspace_root: Path) -> None:
    config_path = workspace_root / ".boxteam" / "boxteam.jsonc"
    payload = commentjson.loads(config_path.read_text(encoding="utf-8"))
    payload["gateway"] = {"workspaces": []}
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_gateway_routes_remote_browser_and_terminal_services(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    docker_e2e_paths: GatewayTargetE2EPaths,
):
    if os.getenv("BOXTEAM_RUN_DOCKER_GATEWAY_E2E") != "1":
        pytest.skip("设置 BOXTEAM_RUN_DOCKER_GATEWAY_E2E=1 后运行 Docker SSH 辅助服务 e2e")
    docker_error = docker_daemon_error()
    if docker_error is not None:
        pytest.skip(f"Docker daemon 当前不可访问: {docker_error}")

    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    local_workspace = Path(e2e_workspace_root_path).resolve()
    gateway_port = port_block.port(41)
    ssh_port = port_block.port(42)
    remote_gateway_port = port_block.port(43)
    terminal_frontend_port = port_block.port(50)
    browser_frontend_port = port_block.port(51)
    tunnel_port_range = (port_block.port(60), port_block.port(75))
    remote_workspace_path = docker_e2e_paths.remote_workspace
    remote_boxteam_home = docker_e2e_paths.remote_boxteam_home

    gateway: GatewayProcess | None = None
    terminal_frontend: TerminalFrontendProcess | None = None
    browser_frontend: BrowserFrontendProcess | None = None
    remote_gateway_pid: str | None = None
    docker_target: GatewaySshTarget | None = None

    try:
        docker_target = ensure_gateway_ssh_container(
            known_hosts_path=(
                local_workspace.parent / "artifacts" / "gateway_ssh_known_hosts"
            ),
        )
        remote_gateway_pid = start_remote_gateway_via_ssh(
            target=docker_target,
            remote_workspace_path=remote_workspace_path,
            remote_gateway_port=remote_gateway_port,
            remote_boxteam_home=remote_boxteam_home,
        )
        write_gateway_remote_gateway_config(
            workspace_root=local_workspace,
            ssh_port=docker_target.ssh_port,
            username=docker_target.username,
            remote_gateway_port=remote_gateway_port,
            private_key_path=install_gateway_ssh_assets_for_e2e(local_workspace),
        )
        terminal_frontend = start_terminal_frontend_process(
            workspace_root=local_workspace,
            frontend_port=terminal_frontend_port,
        )
        browser_frontend = start_browser_frontend_process(
            workspace_root=local_workspace,
            frontend_port=browser_frontend_port,
        )
        gateway_env = {
            "BOXTEAM_TERMINAL_FRONTEND_URL": f"http://127.0.0.1:{terminal_frontend_port}",
            "BOXTEAM_BROWSER_FRONTEND_URL": f"http://127.0.0.1:{browser_frontend_port}",
            "BOXTEAM_GATEWAY_SSH_KNOWN_HOSTS_FILE": str(
                docker_target.known_hosts_path
            ),
            "BOXTEAM_REMOTE_PAIR_COMMAND": build_remote_pair_command(
                docker_target,
                remote_boxteam_home=remote_boxteam_home,
            ),
        }
        gateway = start_gateway_process(
            workspace_root=local_workspace,
            default_backend_url="managed-by-gateway",
            port=gateway_port,
            ssh_tunnel_port_range=tunnel_port_range,
            extra_env=gateway_env,
        )

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            workspace_list = (await client.get("/api/gateway/workspaces")).json()["data"]
            remote_workspace = next(
                item
                for item in workspace_list["items"]
                if item["connection_kind"] == "remote_gateway"
                and item["root_path"] == remote_workspace_path
            )
            assert remote_workspace["status"] == "ready"
            assert remote_workspace["services"]["terminal_manager"]["status"] == "ready"
            assert remote_workspace["services"]["browser_manager"]["status"] == "ready"
            workspace_id = remote_workspace["workspace_id"]

            session_response = await client.post(
                "/api/v1/sessions",
                headers={"X-BoxTeam-Workspace-Id": workspace_id},
                json={"title": "Docker Remote Auxiliary Services"},
            )
            assert session_response.status_code == 200, session_response.text
            session_id = session_response.json()["data"]["session_id"]

            browser_response = await client.post(
                f"/api/gateway/workspaces/{workspace_id}/browser-manager/api/browsers",
                json={
                    "session_id": session_id,
                    "title": "Remote Docker Browser",
                    "url": browser_test_data_url(),
                },
            )
            assert browser_response.status_code == 200, browser_response.text
            browser_id = browser_response.json()["data"]["browser_id"]

            terminal_response = await client.post(
                f"/api/gateway/workspaces/{workspace_id}/terminal-manager/api/terminals",
                json={
                    "session_id": session_id,
                    "title": "Remote Docker Terminal",
                    "cwd": remote_workspace_path,
                    "cols": 100,
                    "rows": 30,
                },
            )
            assert terminal_response.status_code == 200, terminal_response.text
            terminal_id = terminal_response.json()["data"]["terminal_id"]

            resources_response = await client.get(
                f"/api/v1/sessions/{session_id}/resources",
                headers={"X-BoxTeam-Workspace-Id": workspace_id},
            )
            assert resources_response.status_code == 200, resources_response.text
            resources = resources_response.json()["data"]["items"]
            routed_resources = {
                (item["kind"], item["resource_id"]): item
                for item in resources
            }
            assert ("browser", browser_id) in routed_resources
            assert ("terminal", terminal_id) in routed_resources
            assert "attach_url" not in routed_resources[("browser", browser_id)]["metadata"]
            assert "attach_url" not in routed_resources[("terminal", terminal_id)]["metadata"]

            browser_attach = await client.get(
                "/api/gateway/attach/browser/",
                params={"workspaceId": workspace_id, "browserId": browser_id},
            )
            terminal_attach = await client.get(
                "/api/gateway/attach/terminal/",
                params={"workspaceId": workspace_id, "terminalId": terminal_id},
            )
            assert browser_attach.status_code == 200
            assert "可附加浏览器" in browser_attach.text
            assert terminal_attach.status_code == 200
            assert "持久终端" in terminal_attach.text

            browser_ws_url = (
                f"ws://127.0.0.1:{gateway.port}/api/gateway/workspaces/"
                f"{workspace_id}/browser-manager/browser?token=local-dev-token"
            )
            async with websockets.connect(browser_ws_url) as websocket:
                await websocket.send(
                    json.dumps({"type": "attach", "browserId": browser_id})
                )
                attached = await _receive_websocket_type(websocket, "attached")
                assert attached["browserId"] == browser_id
                await websocket.send(
                    json.dumps({"type": "detach", "browserId": browser_id})
                )
                await _receive_websocket_type(websocket, "detached")

            terminal_ws_url = (
                f"ws://127.0.0.1:{gateway.port}/api/gateway/workspaces/"
                f"{workspace_id}/terminal-manager/terminal?token=local-dev-token"
            )
            async with websockets.connect(terminal_ws_url) as websocket:
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
                await _receive_websocket_type(websocket, "attached")
                await websocket.send(
                    json.dumps(
                        {
                            "type": "input",
                            "data": "printf 'GATEWAY_TERMINAL_OK\\n'\r",
                        }
                    )
                )
                await _receive_terminal_output(websocket, "GATEWAY_TERMINAL_OK")

        close_gateway_process(gateway)
        gateway = None
        _remove_declared_remote_gateway(local_workspace)
        gateway = start_gateway_process(
            workspace_root=local_workspace,
            default_backend_url="managed-by-gateway",
            port=gateway_port,
            ssh_tunnel_port_range=tunnel_port_range,
            extra_env=gateway_env,
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as restarted_client:
            restored_response = await restarted_client.get(
                f"/api/gateway/workspaces/{workspace_id}/browser-manager/api/browsers/{browser_id}"
            )
            assert restored_response.status_code == 200, restored_response.text
            assert restored_response.json()["data"]["browser_id"] == browser_id
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        if remote_gateway_pid is not None and docker_target is not None:
            stop_remote_backend(docker_target, remote_gateway_pid)
        if browser_frontend is not None:
            close_browser_frontend_process(browser_frontend)
        if terminal_frontend is not None:
            close_terminal_frontend_process(terminal_frontend)
