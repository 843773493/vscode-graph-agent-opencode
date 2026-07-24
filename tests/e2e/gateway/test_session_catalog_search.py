from __future__ import annotations

import asyncio
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
from tests.e2e.processes import close_backend_process, start_backend_process


@pytest.mark.asyncio
async def test_gateway_aggregates_catalog_breadcrumbs_and_reports_offline_workspace(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
) -> None:
    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(e2e_workspace_root_path).resolve()
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
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            folder_response = await client.post(
                "/api/v1/session-catalog/folders",
                json={"name": "主工作区报告"},
            )
            assert folder_response.status_code == 200, folder_response.text
            primary_folder_id = folder_response.json()["data"]["items"][-1][
                "folder_id"
            ]
            session_response = await client.post(
                "/api/v1/sessions",
                json={"title": "Shared Catalog Needle Primary"},
            )
            assert session_response.status_code == 200, session_response.text
            primary_session_id = session_response.json()["data"]["session_id"]
            assign_response = await client.put(
                f"/api/v1/session-catalog/sessions/{primary_session_id}/folder",
                json={"folder_id": primary_folder_id},
            )
            assert assign_response.status_code == 200, assign_response.text
            primary_storage_relative_path = assign_response.json()["data"]["items"][
                -1
            ]["storage_relative_path"]
    finally:
        close_backend_process(primary_backend)

    secondary_port = port_block.port(1)
    secondary_folder_id = "folder_secondary_reports"
    secondary_session_id = "ses_secondary_needle"
    secondary_folder_storage = (
        f"次工作区报告--{secondary_folder_id[-8:]}"
    )
    secondary_session_storage = (
        f"{secondary_folder_storage}/"
        f"Shared Catalog Needle Secondary--{secondary_session_id[-8:]}"
    )
    secondary_catalog_items = [
        {
            "node_id": secondary_folder_id,
            "kind": "folder",
            "name": "次工作区报告",
            "parent_node_id": None,
            "session_id": None,
            "folder_id": secondary_folder_id,
            "has_children": True,
            "storage_relative_path": secondary_folder_storage,
            "created_at": None,
            "updated_at": None,
        },
        {
            "node_id": secondary_session_id,
            "kind": "session",
            "name": "Shared Catalog Needle Secondary",
            "parent_node_id": secondary_folder_id,
            "session_id": secondary_session_id,
            "folder_id": None,
            "has_children": False,
            "storage_relative_path": secondary_session_storage,
            "created_at": None,
            "updated_at": None,
        },
    ]
    with generation_target_stub(
        secondary_port,
        output_workspace_id="gw_stub_secondary",
        catalog_items=secondary_catalog_items,
        catalog_revision="rev_secondary_export_e2e",
    ) as secondary_state:
        gateway = start_gateway_process(
            workspace_root=primary_workspace,
            default_backend_url="http://127.0.0.1:9",
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
                secondary_workspace_id = next(
                    item["workspace_id"]
                    for item in add_response.json()["data"]["items"]
                    if Path(item["root_path"]).resolve() == secondary_workspace
                )

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
                assert {
                    item["workspace_id"] for item in aggregate["items"]
                } == {primary_workspace_id, secondary_workspace_id}
                assert {
                    item["relative_path"] for item in aggregate["items"]
                } == {
                    "主工作区报告/Shared Catalog Needle Primary",
                    "次工作区报告/Shared Catalog Needle Secondary",
                }
                storage_by_workspace = {
                    item["workspace_id"]: item["storage_relative_path"]
                    for item in aggregate["items"]
                }
                assert storage_by_workspace == {
                    primary_workspace_id: primary_storage_relative_path,
                    secondary_workspace_id: secondary_session_storage,
                }
                assert primary_storage_relative_path != (
                    f"{primary_session_id}"
                )
                assert all(
                    status["status"] == "available"
                    for status in aggregate["workspaces"]
                )

                export_requests = secondary_state.requests_for(
                    "GET", "/api/v1/session-catalog/export"
                )
                assert export_requests
                assert not any(
                    str(record["path"]).startswith(
                        "/api/v1/session-catalog/search"
                    )
                    for record in secondary_state.requests
                )

                cache_dir = (
                    primary_workspace
                    / ".boxteam"
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
        default_backend_url="http://127.0.0.1:9",
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
            assert {
                item["workspace_id"] for item in offline["items"]
            } == {primary_workspace_id, secondary_workspace_id}
            assert {
                item["workspace_id"]: item["storage_relative_path"]
                for item in offline["items"]
            } == {
                primary_workspace_id: primary_storage_relative_path,
                secondary_workspace_id: secondary_session_storage,
            }
            status_by_workspace = {
                item["workspace_id"]: item for item in offline["workspaces"]
            }
            assert status_by_workspace[primary_workspace_id]["status"] == "available"
            assert status_by_workspace[secondary_workspace_id]["status"] == "stale"
            assert status_by_workspace[secondary_workspace_id]["error"]
    finally:
        close_gateway_process(gateway)
