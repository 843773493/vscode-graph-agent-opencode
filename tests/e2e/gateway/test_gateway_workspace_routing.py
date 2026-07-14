from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from tests.e2e.gateway.processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
    workspace_root_from_response,
)
from tests.e2e.ports import e2e_port_block_for_file
from tests.e2e.processes import (
    close_backend_process,
    start_backend_process,
)


def _prepare_workspace(path: Path, name: str) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    return path



@pytest.mark.asyncio
async def test_gateway_routes_sessions_between_local_workspaces(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
):
    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(e2e_workspace_root_path).resolve()
    secondary_workspace = _prepare_workspace(
        primary_workspace.parent / "test_gateway_workspace_routing_secondary",
        "secondary workspace",
    )

    primary_backend = start_backend_process(
        workspace_root=str(primary_workspace),
        port=port_block.port(0),
        log_name="gateway-primary-backend",
    )
    secondary_backend = start_backend_process(
        workspace_root=str(secondary_workspace),
        port=port_block.port(1),
        log_name="gateway-secondary-backend",
    )
    gateway = start_gateway_process(
        workspace_root=primary_workspace,
        default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
        port=port_block.port(2),
    )

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            default_workspace_response = await client.get("/api/v1/workspace")
            assert Path(workspace_root_from_response(default_workspace_response)).resolve() == primary_workspace

            add_response = await client.post(
                "/api/gateway/workspaces/local",
                json={
                    "root_path": str(secondary_workspace),
                    "name": "secondary",
                    "backend_url": f"http://127.0.0.1:{secondary_backend.port}",
                },
            )
            assert add_response.status_code == 200, add_response.text
            workspace_list = add_response.json()["data"]
            default_workspace_id = next(
                item["workspace_id"]
                for item in workspace_list["items"]
                if Path(item["root_path"]).resolve() == primary_workspace
            )
            assert workspace_list["active_workspace_id"] == default_workspace_id
            default_workspace_item = next(
                item
                for item in workspace_list["items"]
                if item["workspace_id"] == default_workspace_id
            )
            assert default_workspace_item["system_default"] is True
            assert default_workspace_item["removable"] is False
            assert workspace_list["items"][0]["workspace_id"] == default_workspace_id
            secondary_workspace_item = next(
                item
                for item in workspace_list["items"]
                if Path(item["root_path"]).resolve() == secondary_workspace
            )
            secondary_workspace_id = secondary_workspace_item["workspace_id"]
            assert secondary_workspace_id
            assert secondary_workspace_item["system_default"] is False
            assert secondary_workspace_item["removable"] is True

            reorder_response = await client.put(
                "/api/gateway/workspaces/order",
                json={"workspace_ids": [secondary_workspace_id, default_workspace_id]},
            )
            assert reorder_response.status_code == 200, reorder_response.text
            reordered_list = reorder_response.json()["data"]
            assert reordered_list["active_workspace_id"] == default_workspace_id
            assert [
                item["workspace_id"]
                for item in reordered_list["items"]
            ][:2] == [secondary_workspace_id, default_workspace_id]

            routed_workspace_response = await client.get("/api/v1/workspace")
            assert Path(workspace_root_from_response(routed_workspace_response)).resolve() == primary_workspace

            create_response = await client.post(
                "/api/v1/sessions",
                json={"title": "Gateway Default Session"},
            )
            assert create_response.status_code == 200, create_response.text
            default_session_id = create_response.json()["data"]["session_id"]

            default_sessions_response = await client.get("/api/v1/sessions")
            assert default_sessions_response.status_code == 200
            default_titles = [
                item["title"]
                for item in default_sessions_response.json()["data"]["items"]
            ]
            assert "Gateway Default Session" in default_titles

            routed_create_response = await client.post(
                "/api/v1/sessions",
                headers={"X-BoxTeam-Workspace-Id": secondary_workspace_id},
                json={"title": "Gateway Routed Session"},
            )
            assert routed_create_response.status_code == 200, routed_create_response.text
            routed_session_id = routed_create_response.json()["data"]["session_id"]

            secondary_sessions_response = await client.get(
                "/api/v1/sessions",
                headers={"X-BoxTeam-Workspace-Id": secondary_workspace_id},
            )
            assert secondary_sessions_response.status_code == 200
            secondary_titles = [
                item["title"]
                for item in secondary_sessions_response.json()["data"]["items"]
            ]
            assert "Gateway Routed Session" in secondary_titles

            primary_sessions_response = await client.get("/api/v1/sessions")
            assert primary_sessions_response.status_code == 200
            primary_session_ids = [
                item["session_id"]
                for item in primary_sessions_response.json()["data"]["items"]
            ]
            assert default_session_id in primary_session_ids
            assert routed_session_id not in primary_session_ids

            delete_default_response = await client.delete(
                f"/api/gateway/workspaces/{default_workspace_id}"
            )
            assert delete_default_response.status_code == 403

            delete_secondary_response = await client.delete(
                f"/api/gateway/workspaces/{secondary_workspace_id}"
            )
            assert delete_secondary_response.status_code == 200, delete_secondary_response.text
            after_delete_items = delete_secondary_response.json()["data"]["items"]
            assert all(
                item["workspace_id"] != secondary_workspace_id
                for item in after_delete_items
            )
    finally:
        close_gateway_process(gateway)
        close_backend_process(secondary_backend)
        close_backend_process(primary_backend)

