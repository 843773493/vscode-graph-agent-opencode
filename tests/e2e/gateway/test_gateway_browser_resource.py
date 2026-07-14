from __future__ import annotations

import os
from pathlib import Path
from urllib.request import urlopen

import httpx
import pytest

from tests.e2e.gateway.browser_manager import (
    BrowserManagerProcesses,
    browser_test_data_url,
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.e2e.gateway.gateway_docker import (
    GatewaySshDockerTarget,
    container_host_gateway,
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
    write_gateway_ssh_workspace_config,
)
from tests.e2e.ports import e2e_port_block_for_file
from tests.e2e.processes import close_backend_process, start_backend_process


@pytest.mark.asyncio
async def test_gateway_ssh_workspace_exposes_host_managed_browser_resource(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
):
    if os.getenv("BOXTEAM_RUN_DOCKER_GATEWAY_E2E") != "1":
        pytest.skip("设置 BOXTEAM_RUN_DOCKER_GATEWAY_E2E=1 后运行 Docker SSH 跨端浏览器 e2e")
    docker_error = docker_daemon_error()
    if docker_error is not None:
        pytest.skip(f"Docker daemon 当前不可访问: {docker_error}")

    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    local_workspace = Path(e2e_workspace_root_path).resolve()
    local_backend_port = port_block.port(40)
    gateway_port = port_block.port(41)
    ssh_port = port_block.port(42)
    remote_backend_port = local_backend_port
    browser_backend_port = port_block.port(50)
    browser_frontend_port = port_block.port(51)
    tunnel_port_range = (port_block.port(60), port_block.port(69))
    remote_workspace_path = f"/tmp/boxteam-gateway-browser-workspace-{ssh_port}"

    local_backend = start_backend_process(
        workspace_root=str(local_workspace),
        port=local_backend_port,
        log_name="gateway-browser-local-backend",
    )
    gateway: GatewayProcess | None = None
    browser_manager: BrowserManagerProcesses | None = None
    remote_backend_pid: str | None = None
    docker_target: GatewaySshDockerTarget | None = None

    try:
        docker_target = start_gateway_ssh_container(ssh_port=ssh_port)
        host_gateway = container_host_gateway(docker_target)
        browser_manager = start_browser_manager_processes(
            workspace_root=local_workspace,
            backend_port=browser_backend_port,
            frontend_port=browser_frontend_port,
            backend_host="0.0.0.0",
        )
        remote_backend_pid = start_remote_backend_via_ssh(
            target=docker_target,
            remote_workspace_path=remote_workspace_path,
            remote_backend_port=remote_backend_port,
            extra_env={
                "BOXTEAM_BROWSER_BACKEND_URL": f"http://{host_gateway}:{browser_backend_port}",
                "BOXTEAM_BROWSER_FRONTEND_URL": f"http://127.0.0.1:{browser_frontend_port}",
            },
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
            ssh_workspace = next(
                item
                for item in workspace_list["items"]
                if item["connection_kind"] == "ssh"
                and item["root_path"] == remote_workspace_path
                and item["status"] == "ready"
            )
            ssh_workspace_id = ssh_workspace["workspace_id"]

            create_response = await client.post(
                "/api/v1/sessions",
                headers={"X-BoxTeam-Workspace-Id": ssh_workspace_id},
                json={"title": "Docker Browser Routed Session"},
            )
            assert create_response.status_code == 200, create_response.text
            session_id = create_response.json()["data"]["session_id"]

            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{browser_backend_port}",
                timeout=60,
            ) as browser_client:
                browser_response = await browser_client.post(
                    "/api/browsers",
                    json={
                        "session_id": session_id,
                        "title": "Docker Routed Browser",
                        "url": browser_test_data_url(),
                    },
                )
            assert browser_response.status_code == 200, browser_response.text
            browser_id = browser_response.json()["data"]["browser_id"]

            resources_response = await client.get(
                f"/api/v1/sessions/{session_id}/resources",
                headers={"X-BoxTeam-Workspace-Id": ssh_workspace_id},
            )
            assert resources_response.status_code == 200, resources_response.text
            resources = resources_response.json()["data"]["items"]
            browser_resource = next(
                item
                for item in resources
                if item["kind"] == "browser" and item["resource_id"] == browser_id
            )
            assert browser_resource["status"] == "running"
            assert browser_resource["metadata"]["attach_url"] == (
                f"http://127.0.0.1:{browser_frontend_port}/?browserId={browser_id}"
            )

            with urlopen(browser_resource["metadata"]["attach_url"], timeout=5) as response:
                html = response.read().decode("utf-8")
            assert response.status == 200
            assert "可附加浏览器" in html
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        if remote_backend_pid is not None and docker_target is not None:
            stop_remote_backend(docker_target, remote_backend_pid)
        if browser_manager is not None:
            close_browser_manager_processes(browser_manager)
        if docker_target is not None:
            stop_gateway_ssh_container(docker_target)
        close_backend_process(local_backend)
