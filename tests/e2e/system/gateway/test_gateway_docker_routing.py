from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import commentjson
import httpx
import pytest

from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextSearchRequest,
)
from app.services.business.gateway_context_query_service import (
    GatewayContextQueryService,
)
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from tests.e2e.system.gateway.gateway_docker import (
    docker_daemon_error,
    ensure_gateway_ssh_container,
)
from tests.e2e.system.gateway.gateway_target import (
    GatewaySshTarget,
    GatewayTargetE2EPaths,
    build_remote_pair_command,
    install_gateway_ssh_assets_for_e2e,
    start_remote_gateway_via_ssh,
    stop_remote_backend,
)
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
    workspace_root_from_response,
    write_gateway_remote_gateway_config,
)
from tests.support.ports import e2e_port_block_for_file


@pytest.mark.asyncio
async def test_gateway_federates_complete_remote_gateway_through_docker(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    docker_e2e_paths: GatewayTargetE2EPaths,
):
    if os.getenv("BOXTEAM_RUN_DOCKER_GATEWAY_E2E") != "1":
        pytest.skip("设置 BOXTEAM_RUN_DOCKER_GATEWAY_E2E=1 后运行 Docker SSH 跨端 e2e")
    docker_error = docker_daemon_error()
    if docker_error is not None:
        pytest.skip(f"Docker daemon 当前不可访问: {docker_error}")

    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    local_workspace = Path(e2e_workspace_root_path).resolve()
    gateway_port = port_block.port(21)
    remote_gateway_port = port_block.port(23)
    tunnel_port_range = (port_block.port(30), port_block.port(39))
    remote_workspace_path = docker_e2e_paths.remote_workspace
    remote_boxteam_home = docker_e2e_paths.remote_boxteam_home

    gateway: GatewayProcess | None = None
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
            remote_pair_command=build_remote_pair_command(
                docker_target,
                remote_boxteam_home=remote_boxteam_home,
            ),
        )
        gateway = start_gateway_process(
            workspace_root=local_workspace,
            default_backend_url="managed-by-gateway",
            port=gateway_port,
            ssh_tunnel_port_range=tunnel_port_range,
            extra_env={
                "BOXTEAM_GATEWAY_SSH_KNOWN_HOSTS_FILE": str(
                    docker_target.known_hosts_path
                ),
            },
        )

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
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
            remote_workspace = next(
                item
                for item in workspace_list["items"]
                if item["connection_kind"] == "remote_gateway"
                and item["root_path"] == remote_workspace_path
                and item["status"] == "ready"
            )
            remote_workspace_id = remote_workspace["workspace_id"]
            assert workspace_list["active_workspace_id"] != remote_workspace_id
            tunnel_port = urlparse(remote_workspace["backend_url"]).port
            assert tunnel_port is not None
            assert tunnel_port_range[0] <= tunnel_port <= tunnel_port_range[1]
            assert tunnel_port != remote_gateway_port

            ssh_connections_response = await client.get(
                "/api/gateway/ssh-connections"
            )
            assert ssh_connections_response.status_code == 200, (
                ssh_connections_response.text
            )
            configured_connection = next(
                item
                for item in ssh_connections_response.json()["data"]["items"]
                if item["source"] == "boxteam"
                and item["workspace_id"] == remote_workspace_id
            )
            assert configured_connection["initial_path"] == remote_workspace_path
            assert configured_connection["ssh_config_host"] is None

            default_workspace_response = await client.get("/api/v1/workspace")
            assert Path(workspace_root_from_response(default_workspace_response)).resolve() == local_workspace

            remote_workspace_response = await client.get(
                "/api/v1/workspace",
                headers={"X-BoxTeam-Workspace-Id": remote_workspace_id},
            )
            assert Path(workspace_root_from_response(remote_workspace_response)).as_posix() == remote_workspace_path

            remote_tools_response = await client.get(
                "/api/v1/tools",
                params={"agent_id": "default"},
                headers={"X-BoxTeam-Workspace-Id": remote_workspace_id},
            )
            assert remote_tools_response.status_code == 200, remote_tools_response.text
            remote_tool_ids = {
                item["tool_id"] for item in remote_tools_response.json()["data"]
            }
            assert {
                "read_context",
                "search_context",
                "openBrowserPage",
                "runPlaywrightCode",
            } <= remote_tool_ids
            remote_tool_payload = remote_tools_response.json()["data"]
            assert all(
                {"execution_enabled", "model_visible"} <= item.keys()
                for item in remote_tool_payload
            )
            assert remote_tools_response.headers.get("X-Request-ID")

            remote_tool_tests_response = await client.get(
                "/api/v1/tools/tests",
                params={"limit": 50},
                headers={"X-BoxTeam-Workspace-Id": remote_workspace_id},
            )
            assert remote_tool_tests_response.status_code == 200, remote_tool_tests_response.text
            assert isinstance(remote_tool_tests_response.json()["data"]["items"], list)

            create_response = await client.post(
                "/api/v1/sessions",
                headers={"X-BoxTeam-Workspace-Id": remote_workspace_id},
                json={"title": "Docker SSH Routed Session"},
            )
            assert create_response.status_code == 200, create_response.text
            remote_session_id = create_response.json()["data"]["session_id"]

            projected_sessions_resource = (
                f"boxteam://workspace/{remote_workspace_id}/sessions"
            )
            context_search_response = await client.post(
                "/api/v1/context/search",
                headers={"X-BoxTeam-Workspace-Id": remote_workspace_id},
                json={
                    "resource": projected_sessions_resource,
                    "query": "Docker SSH Routed Session",
                    "sources": ["session_catalog"],
                },
            )
            assert context_search_response.status_code == 200, (
                context_search_response.text
            )
            context_matches = context_search_response.json()["data"]["matches"]
            projected_locator = next(
                item["locator"]
                for item in context_matches
                if remote_session_id in item["locator"]
            )
            assert projected_locator.startswith(
                f"boxteam://workspace/{remote_workspace_id}/session/"
            )

            context_read_response = await client.post(
                "/api/v1/context/read",
                headers={"X-BoxTeam-Workspace-Id": remote_workspace_id},
                json={"resource": projected_locator},
            )
            assert context_read_response.status_code == 200, context_read_response.text
            assert context_read_response.json()["data"]["resource"] == projected_locator

            sessions_response = await client.get(
                "/api/v1/sessions",
                headers={"X-BoxTeam-Workspace-Id": remote_workspace_id},
            )
            assert sessions_response.status_code == 200, sessions_response.text
            session_titles = [
                item["title"]
                for item in sessions_response.json()["data"]["items"]
            ]
            assert "Docker SSH Routed Session" in session_titles

            activate_response = await client.post(
                f"/api/gateway/workspaces/{remote_workspace_id}/activate"
            )
            assert activate_response.status_code == 200, activate_response.text

        close_gateway_process(gateway)
        gateway = None
        config_path = local_workspace / ".boxteam" / "workspace.jsonc"
        config_payload = commentjson.loads(config_path.read_text(encoding="utf-8"))
        config_payload["gateway"] = {"workspaces": []}
        config_path.write_text(
            json.dumps(config_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        gateway = start_gateway_process(
            workspace_root=local_workspace,
            default_backend_url="managed-by-gateway",
            port=gateway_port,
            ssh_tunnel_port_range=tunnel_port_range,
            extra_env={
                "BOXTEAM_GATEWAY_SSH_KNOWN_HOSTS_FILE": str(
                    docker_target.known_hosts_path
                ),
            },
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as restarted_client:
            restored_response = await restarted_client.get("/api/gateway/workspaces")
            assert restored_response.status_code == 200, restored_response.text
            restored_list = restored_response.json()["data"]
            restored_ssh = next(
                item
                for item in restored_list["items"]
                if item["workspace_id"] == remote_workspace_id
            )
            assert restored_ssh["status"] == "ready"
            assert restored_ssh["connection_error"] is None
            assert restored_list["active_workspace_id"] == remote_workspace_id

        context_service = GatewayContextQueryService(
            transport=GatewaySessionContextClient(
                gateway_url=f"http://127.0.0.1:{gateway.port}"
            )
        )
        gateway_inventory = await context_service.read_gateway_context(
            SessionContextReadRequest(
                resource="boxteam://gateway/workspaces",
                view="inventory",
            )
        )
        assert any(
            item.locator == f"boxteam://workspace/{remote_workspace_id}"
            for item in gateway_inventory.items
        )

        stop_remote_backend(docker_target, remote_gateway_pid)
        remote_gateway_pid = None
        offline_search = await context_service.search_gateway_context(
            SessionContextSearchRequest(
                resource="boxteam://gateway",
                query="Docker SSH Routed Session",
                sources=["session_catalog"],
            )
        )
        assert any(
            error.resource == f"boxteam://workspace/{remote_workspace_id}"
            for error in offline_search.partial_errors
        )
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        if remote_gateway_pid is not None and docker_target is not None:
            stop_remote_backend(docker_target, remote_gateway_pid)
