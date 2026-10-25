from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver
from tests.integration.stubs.http_stubs import generation_target_stub
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
    workspace_backend_id_for_root,
)
from tests.support.session_navigation.http_operations import submit_navigation_operation


@pytest.mark.asyncio
async def test_gateway_aggregates_catalog_breadcrumbs_and_reports_offline_workspace(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
) -> None:
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(integration_workspace_root_path).resolve()
    secondary_workspace = primary_workspace / "catalog-secondary-workspace"
    secondary_workspace.mkdir(parents=True, exist_ok=True)
    (secondary_workspace / "README.md").write_text(
        "# catalog secondary\n",
        encoding="utf-8",
    )
    primary_backend = start_backend_process(
        workspace_root=str(primary_workspace),
        port=port_block.port(0),
        log_name="catalog-primary-backend",
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{primary_backend.port}",
            headers={
                **LOCAL_TOKEN_HEADERS,
                "X-BoxTeam-Workspace-Id": primary_backend.workspace_id,
                "X-BoxTeam-Gateway-Id": "gateway_0123456789abcdef0123456789abcdef",
                "X-BoxTeam-Navigation-Actor": "guest",
            },
            timeout=30,
        ) as client:
            folder_receipt = await submit_navigation_operation(
                client,
                "create_folder",
                name="主工作区报告",
            )
            assert folder_receipt["state"] == "committed"
            primary_folder_id = str(folder_receipt["created_node_id"])
            session_response = await client.post(
                "/api/v1/sessions",
                json={"title": "Shared Catalog Needle Primary"},
            )
            assert session_response.status_code == 200, session_response.text
            primary_session_id = session_response.json()["data"]["session_id"]
            assign_receipt = await submit_navigation_operation(
                client,
                "move_node",
                target_node_id=primary_session_id,
                parent_node_id=primary_folder_id,
            )
            assert assign_receipt["state"] == "committed"
            export_response = await client.get("/api/v1/session-catalog/export")
            assert export_response.status_code == 200, export_response.text
            primary_session = next(
                item
                for item in export_response.json()["data"]["items"]
                if item["node_id"] == primary_session_id
            )
            assert "storage_relative_path" not in primary_session
            resolver = get_session_path_resolver(
                primary_workspace / ".boxteam/sessions"
            )
            primary_storage_relative_path = (
                resolver.resolve_session_node(primary_session_id)
                .relative_to(primary_workspace / ".boxteam/sessions")
                .as_posix()
            )
    finally:
        close_backend_process(primary_backend)

    secondary_port = port_block.port(1)
    secondary_folder_id = create_prefixed_id("ses")
    secondary_session_id = create_prefixed_id("ses")
    secondary_created_at = datetime.now(UTC).replace(microsecond=0)
    secondary_session_storage = (
        f"{secondary_created_at:%Y/%m/%d}/{secondary_session_id}"
    )
    secondary_backend_workspace_id = workspace_backend_id_for_root(secondary_workspace)
    secondary_catalog_items = [
        {
            "node_id": secondary_folder_id,
            "kind": "folder",
            "name": "次工作区报告",
            "parent_node_id": None,
            "session_id": None,
            "folder_id": secondary_folder_id,
            "has_children": True,
            "node_revision": 1,
            "state": "active",
            "created_at": None,
            "updated_at": None,
            "session": None,
        },
        {
            "node_id": secondary_session_id,
            "kind": "session",
            "name": "Shared Catalog Needle Secondary",
            "parent_node_id": secondary_folder_id,
            "session_id": secondary_session_id,
            "folder_id": None,
            "has_children": False,
            "node_revision": 1,
            "state": "active",
            "created_at": secondary_created_at.isoformat().replace("+00:00", "Z"),
            "updated_at": None,
            "session": None,
        },
    ]
    with generation_target_stub(
        secondary_port,
        output_workspace_id="gw_stub_secondary",
        backend_workspace_id=secondary_backend_workspace_id,
        catalog_items=secondary_catalog_items,
        catalog_revision="rev_secondary_export_e2e",
    ) as secondary_state:
        gateway = start_gateway_process(
            workspace_root=primary_workspace,
            default_backend_url=None,
            port=port_block.port(2),
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{gateway.port}",
                headers=LOCAL_TOKEN_HEADERS,
                timeout=30,
            ) as client:
                workspaces_response = await client.get("/api/gateway/workspaces")
                assert workspaces_response.status_code == 200, workspaces_response.text
                primary_workspace_id = workspaces_response.json()["data"][
                    "active_workspace_id"
                ]
                add_response = await client.post(
                    "/api/gateway/workspaces/local",
                    json={
                        "root_path": str(secondary_workspace),
                        "name": "目录聚合次工作区",
                        "backend_url": f"http://127.0.0.1:{secondary_port}",
                    },
                )
                assert add_response.status_code == 200, add_response.text
                registered = [
                    item
                    for item in add_response.json()["data"]["items"]
                    if item["backend_workspace_id"] == secondary_backend_workspace_id
                ]
                assert len(registered) == 1, add_response.text
                assert (
                    registered[0]["backend_url"] == f"http://127.0.0.1:{secondary_port}"
                )
                secondary_workspace_id = registered[0]["workspace_id"]

                aggregate_response = await client.get(
                    "/api/gateway/session-catalog/search",
                    params={
                        "query": "Shared Catalog Needle",
                        "limit_per_workspace": 10,
                    },
                )
                assert aggregate_response.status_code == 200, aggregate_response.text
                aggregate = aggregate_response.json()["data"]
                assert aggregate["total"] == 2
                assert {item["workspace_id"] for item in aggregate["items"]} == {
                    primary_workspace_id,
                    secondary_workspace_id,
                }
                assert {item["relative_path"] for item in aggregate["items"]} == {
                    "主工作区报告/Shared Catalog Needle Primary",
                    "次工作区报告/Shared Catalog Needle Secondary",
                }
                assert {item["session_id"] for item in aggregate["items"]} == {
                    primary_session_id,
                    secondary_session_id,
                }
                assert "storage_relative_path" not in aggregate_response.text
                for hidden in (
                    str(primary_workspace),
                    str(secondary_workspace),
                    primary_storage_relative_path,
                    secondary_session_storage,
                ):
                    assert hidden not in aggregate_response.text
                for workspace_id, session_id, query in (
                    (
                        primary_workspace_id,
                        primary_session_id,
                        primary_storage_relative_path,
                    ),
                    (
                        secondary_workspace_id,
                        secondary_session_id,
                        secondary_session_storage,
                    ),
                ):
                    response = await client.get(
                        "/api/gateway/session-catalog/search",
                        params={"query": query, "limit_per_workspace": 10},
                    )
                    assert response.status_code == 200, response.text
                    assert [
                        (item["workspace_id"], item["session_id"])
                        for item in response.json()["data"]["items"]
                    ] == [(workspace_id, session_id)]
                    assert query not in response.text
                    assert "storage_relative_path" not in response.text
                assert all(
                    status["status"] == "available"
                    for status in aggregate["workspaces"]
                )

                export_requests = secondary_state.requests_for(
                    "GET", "/api/v1/session-catalog/export"
                )
                assert export_requests
                assert not any(
                    str(record["path"]).startswith("/api/v1/session-catalog/search")
                    for record in secondary_state.requests
                )

                # Gateway 控制面数据（含目录索引缓存）位于隔离 BOXTEAM_HOME，
                # 不写入工作区 .boxteam/。
                cache_dir = (
                    primary_workspace.parent
                    / "boxteam-home"
                    / "state"
                    / "gateway"
                    / "indexes"
                    / "session-catalogs"
                )
                cached_indexes = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in cache_dir.glob("*.json")
                ]
                cached_by_workspace = {
                    item["workspace_id"]: item for item in cached_indexes
                }
                assert set(cached_by_workspace) == {
                    primary_workspace_id,
                    secondary_workspace_id,
                }
                assert all(item["schema_version"] == 2 for item in cached_indexes)
                cache_json = json.dumps(cached_indexes)
                assert "storage_relative_path" not in cache_json
                for hidden in (
                    str(primary_workspace),
                    str(secondary_workspace),
                    primary_storage_relative_path,
                    secondary_session_storage,
                ):
                    assert hidden not in cache_json
                assert cached_by_workspace[secondary_workspace_id]["revision"] == (
                    "rev_secondary_export_e2e"
                )
                assert cached_by_workspace[secondary_workspace_id]["items"] == (
                    secondary_catalog_items
                )
        finally:
            close_gateway_process(gateway)

    gateway = start_gateway_process(
        workspace_root=primary_workspace,
        default_backend_url=None,
        port=port_block.port(2),
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            offline: dict[str, object] | None = None
            for _attempt in range(50):
                offline_response = await client.get(
                    "/api/gateway/session-catalog/search",
                    params={
                        "query": "Shared Catalog Needle",
                        "limit_per_workspace": 10,
                    },
                )
                assert offline_response.status_code == 200, offline_response.text
                offline = offline_response.json()["data"]
                status_by_workspace = {
                    item["workspace_id"]: item for item in offline["workspaces"]
                }
                if status_by_workspace[secondary_workspace_id]["status"] == "stale":
                    break
                await asyncio.sleep(0.1)
            assert offline is not None
            assert {item["workspace_id"] for item in offline["items"]} == {
                primary_workspace_id,
                secondary_workspace_id,
            }
            assert {item["session_id"] for item in offline["items"]} == {
                primary_session_id,
                secondary_session_id,
            }
            assert "storage_relative_path" not in offline_response.text
            assert primary_storage_relative_path not in offline_response.text
            assert secondary_session_storage not in offline_response.text
            status_by_workspace = {
                item["workspace_id"]: item for item in offline["workspaces"]
            }
            assert status_by_workspace[primary_workspace_id]["status"] == "available"
            assert status_by_workspace[secondary_workspace_id]["status"] == "stale"
            assert status_by_workspace[secondary_workspace_id]["error"]
    finally:
        close_gateway_process(gateway)
