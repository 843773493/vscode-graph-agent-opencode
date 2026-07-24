from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests.e2e.gateway.processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.e2e.http_stubs import generation_target_stub
from tests.e2e.ports import e2e_port_block_for_file


def _node_for_workspace(nodes: list[dict[str, object]], workspace_id: str) -> dict[str, object]:
    return next(node for node in nodes if node.get("workspace_id") == workspace_id)


@pytest.mark.asyncio
async def test_workspace_navigation_crud_cycle_nonempty_delete_and_restart(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
) -> None:
    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    workspace_root = Path(e2e_workspace_root_path).resolve()
    secondary_workspace = workspace_root / "secondary-workspace"
    secondary_workspace.mkdir(parents=True, exist_ok=True)
    (secondary_workspace / "README.md").write_text(
        "# workspace navigation secondary\n",
        encoding="utf-8",
    )
    secondary_backend_port = port_block.port(1)
    with generation_target_stub(
        secondary_backend_port,
        output_workspace_id="gw_stub_secondary",
    ):
        gateway = start_gateway_process(
            workspace_root=workspace_root,
            default_backend_url="http://127.0.0.1:9",
            port=port_block.port(0),
        )
        try:
            client = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gateway.port}",
                headers=LOCAL_TOKEN_HEADERS,
                timeout=30,
            )
            workspaces_response = await client.get("/api/gateway/workspaces")
            assert workspaces_response.status_code == 200, workspaces_response.text
            default_workspace_id = workspaces_response.json()["data"][
                "active_workspace_id"
            ]
            add_response = await client.post(
                "/api/gateway/workspaces/local",
                json={
                    "root_path": str(secondary_workspace),
                    "name": "导航测试次工作区",
                    "backend_url": f"http://127.0.0.1:{secondary_backend_port}",
                },
            )
            assert add_response.status_code == 200, add_response.text
            secondary_workspace_id = next(
                item["workspace_id"]
                for item in add_response.json()["data"]["items"]
                if Path(item["root_path"]).resolve() == secondary_workspace
            )

            initial_response = await client.get("/api/gateway/workspace-navigation")
            assert initial_response.status_code == 200, initial_response.text
            initial = initial_response.json()["data"]
            assert initial_response.headers["X-Request-ID"]
            assert initial["revision"]
            assert {
                node["workspace_id"]
                for node in initial["nodes"]
                if node["kind"] == "workspace_ref"
            } == {default_workspace_id, secondary_workspace_id}

            root_folder_response = await client.post(
                "/api/gateway/workspace-navigation/folders",
                json={"name": "客户项目"},
            )
            assert root_folder_response.status_code == 200, root_folder_response.text
            root_folder = next(
                node
                for node in root_folder_response.json()["data"]["nodes"]
                if node["kind"] == "workspace_folder" and node["name"] == "客户项目"
            )
            child_folder_response = await client.post(
                "/api/gateway/workspace-navigation/folders",
                json={
                    "name": "客户甲",
                    "parent_node_id": root_folder["node_id"],
                },
            )
            assert child_folder_response.status_code == 200, child_folder_response.text
            child_folder = next(
                node
                for node in child_folder_response.json()["data"]["nodes"]
                if node["kind"] == "workspace_folder" and node["name"] == "客户甲"
            )
            default_ref = _node_for_workspace(
                child_folder_response.json()["data"]["nodes"],
                default_workspace_id,
            )
            secondary_ref = _node_for_workspace(
                child_folder_response.json()["data"]["nodes"],
                secondary_workspace_id,
            )

            move_default_response = await client.patch(
                f"/api/gateway/workspace-navigation/nodes/{default_ref['node_id']}",
                json={"parent_node_id": child_folder["node_id"]},
            )
            assert move_default_response.status_code == 200, move_default_response.text
            move_secondary_response = await client.patch(
                f"/api/gateway/workspace-navigation/nodes/{secondary_ref['node_id']}",
                json={"parent_node_id": root_folder["node_id"]},
            )
            assert move_secondary_response.status_code == 200, move_secondary_response.text

            breadcrumb_response = await client.get(
                f"/api/gateway/workspace-navigation/nodes/{default_ref['node_id']}/breadcrumb"
            )
            assert breadcrumb_response.status_code == 200, breadcrumb_response.text
            assert [
                item["name"] for item in breadcrumb_response.json()["data"]["items"]
            ] == ["客户项目", "客户甲", default_ref["name"]]

            cycle_response = await client.patch(
                f"/api/gateway/workspace-navigation/nodes/{root_folder['node_id']}",
                json={"parent_node_id": child_folder["node_id"]},
            )
            assert cycle_response.status_code == 400, cycle_response.text
            assert "循环" in cycle_response.text

            nonempty_delete_response = await client.delete(
                f"/api/gateway/workspace-navigation/folders/{root_folder['node_id']}"
            )
            assert nonempty_delete_response.status_code == 409
            assert "非空" in nonempty_delete_response.text

            recursive_delete_response = await client.delete(
                f"/api/gateway/workspace-navigation/folders/{root_folder['node_id']}",
                params={"recursive": "true"},
            )
            assert recursive_delete_response.status_code == 200, (
                recursive_delete_response.text
            )
            recursively_deleted = recursive_delete_response.json()["data"]["nodes"]
            assert root_folder["node_id"] not in {
                node["node_id"] for node in recursively_deleted
            }
            assert child_folder["node_id"] not in {
                node["node_id"] for node in recursively_deleted
            }
            assert _node_for_workspace(
                recursively_deleted, default_workspace_id
            )["parent_node_id"] is None
            assert _node_for_workspace(
                recursively_deleted, secondary_workspace_id
            )["parent_node_id"] is None

            recreated_response = await client.post(
                "/api/gateway/workspace-navigation/folders",
                json={"name": "重启后仍存在"},
            )
            assert recreated_response.status_code == 200, recreated_response.text
            persisted = recreated_response.json()["data"]
            persisted_folder = next(
                node for node in persisted["nodes"] if node["name"] == "重启后仍存在"
            )
            await client.aclose()

            storage_path = (
                workspace_root
                / ".boxteam"
                / "gateway"
                / "navigation"
                / "workspace-tree.json"
            )
            stored = json.loads(storage_path.read_text(encoding="utf-8"))
            assert any(
                node["node_id"] == persisted_folder["node_id"]
                for node in stored["nodes"]
            )

            close_gateway_process(gateway)
            gateway = start_gateway_process(
                workspace_root=workspace_root,
                default_backend_url="http://127.0.0.1:9",
                port=port_block.port(0),
            )
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gateway.port}",
                headers=LOCAL_TOKEN_HEADERS,
                timeout=30,
            ) as restarted_client:
                restored_response = await restarted_client.get(
                    "/api/gateway/workspace-navigation"
                )
                assert restored_response.status_code == 200, restored_response.text
                restored = restored_response.json()["data"]
                assert any(
                    node["node_id"] == persisted_folder["node_id"]
                    and node["name"] == "重启后仍存在"
                    for node in restored["nodes"]
                )
        finally:
            close_gateway_process(gateway)
