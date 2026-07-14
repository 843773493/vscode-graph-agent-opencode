from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from tests.e2e.gateway.gateway_docker import (
    GatewaySshDockerTarget,
    docker_daemon_error,
    start_gateway_ssh_container,
    start_remote_backend_via_ssh,
    stop_gateway_ssh_container,
    stop_remote_backend,
    wait_for_remote_backend_ready,
)
from tests.e2e.gateway.processes import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    close_gateway_process,
    start_gateway_process,
    workspace_root_from_response,
    write_gateway_ssh_workspace_config,
)
from tests.e2e.ports import e2e_port_block_for_file
from tests.e2e.processes import close_backend_process, start_backend_process


@pytest.mark.asyncio
async def test_gateway_loads_configured_ssh_workspace_through_docker(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
):
    if os.getenv("BOXTEAM_RUN_DOCKER_GATEWAY_E2E") != "1":
        pytest.skip("设置 BOXTEAM_RUN_DOCKER_GATEWAY_E2E=1 后运行 Docker SSH 跨端 e2e")
    docker_error = docker_daemon_error()
    if docker_error is not None:
        pytest.skip(f"Docker daemon 当前不可访问: {docker_error}")

    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    local_workspace = Path(e2e_workspace_root_path).resolve()
    local_backend_port = port_block.port(20)
    gateway_port = port_block.port(21)
    ssh_port = port_block.port(22)
    remote_backend_port = local_backend_port
    tunnel_port_range = (port_block.port(30), port_block.port(39))
    remote_workspace_path = f"/tmp/boxteam-gateway-workspace-{ssh_port}"

    local_backend = start_backend_process(
        workspace_root=str(local_workspace),
        port=local_backend_port,
        log_name="gateway-ssh-local-backend",
    )
    gateway: GatewayProcess | None = None
    remote_backend_pid: str | None = None
    docker_target: GatewaySshDockerTarget | None = None

    try:
        docker_target = start_gateway_ssh_container(ssh_port=ssh_port)
        remote_backend_pid = start_remote_backend_via_ssh(
            target=docker_target,
            remote_workspace_path=remote_workspace_path,
            remote_backend_port=remote_backend_port,
        )
        wait_for_remote_backend_ready(
            target=docker_target,
            remote_backend_port=remote_backend_port,
            remote_backend_pid=remote_backend_pid,
        )
        write_gateway_ssh_workspace_config(
            workspace_root=local_workspace,
            ssh_port=ssh_port,
            username=docker_target.username,
            remote_backend_port=remote_backend_port,
            remote_workspace_path=remote_workspace_path,
        )
        gateway = start_gateway_process(
            workspace_root=local_workspace,
            default_backend_url=f"http://127.0.0.1:{local_backend.port}",
            port=gateway_port,
            ssh_tunnel_port_range=tunnel_port_range,
        )

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            list_response = await client.get("/api/gateway/workspaces")
            assert list_response.status_code == 200, list_response.text
            workspace_list = list_response.json()["data"]
            assert len(workspace_list["items"]) >= 2
            assert any(
                item["connection_kind"] == "local"
                and Path(item["root_path"]).resolve() == local_workspace
                and item["status"] == "ready"
                for item in workspace_list["items"]
            )
            ssh_workspace = next(
                item
                for item in workspace_list["items"]
                if item["connection_kind"] == "ssh"
                and item["root_path"] == remote_workspace_path
                and item["status"] == "ready"
            )
            ssh_workspace_id = ssh_workspace["workspace_id"]
            assert workspace_list["active_workspace_id"] != ssh_workspace_id
            tunnel_port = urlparse(ssh_workspace["backend_url"]).port
            assert tunnel_port is not None
            assert tunnel_port_range[0] <= tunnel_port <= tunnel_port_range[1]
            assert tunnel_port != remote_backend_port

            default_workspace_response = await client.get("/api/v1/workspace")
            assert Path(workspace_root_from_response(default_workspace_response)).resolve() == local_workspace

            remote_workspace_response = await client.get(
                "/api/v1/workspace",
                headers={"X-BoxTeam-Workspace-Id": ssh_workspace_id},
            )
            assert Path(workspace_root_from_response(remote_workspace_response)).as_posix() == remote_workspace_path

            remote_tools_response = await client.get(
                "/api/v1/tools",
                params={"agent_id": "default"},
                headers={"X-BoxTeam-Workspace-Id": ssh_workspace_id},
            )
            assert remote_tools_response.status_code == 200, remote_tools_response.text
            remote_tool_ids = {
                item["tool_id"] for item in remote_tools_response.json()["data"]
            }
            assert {
                "read_session_recent_text_messages",
                "grep_session_context_jsonl",
                "read_session_context_jsonl",
                "openBrowserPage",
                "runPlaywrightCode",
            } <= remote_tool_ids

            remote_tool_tests_response = await client.get(
                "/api/v1/tools/tests",
                params={"limit": 50},
                headers={"X-BoxTeam-Workspace-Id": ssh_workspace_id},
            )
            assert remote_tool_tests_response.status_code == 200, remote_tool_tests_response.text
            assert isinstance(remote_tool_tests_response.json()["data"]["items"], list)

            create_response = await client.post(
                "/api/v1/sessions",
                headers={"X-BoxTeam-Workspace-Id": ssh_workspace_id},
                json={"title": "Docker SSH Routed Session"},
            )
            assert create_response.status_code == 200, create_response.text

            sessions_response = await client.get(
                "/api/v1/sessions",
                headers={"X-BoxTeam-Workspace-Id": ssh_workspace_id},
            )
            assert sessions_response.status_code == 200, sessions_response.text
            session_titles = [
                item["title"]
                for item in sessions_response.json()["data"]["items"]
            ]
            assert "Docker SSH Routed Session" in session_titles
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        if remote_backend_pid is not None and docker_target is not None:
            stop_remote_backend(docker_target, remote_backend_pid)
        if docker_target is not None:
            stop_gateway_ssh_container(docker_target)
        close_backend_process(local_backend)
