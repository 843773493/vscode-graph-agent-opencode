from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.tools.custom_invocation import create_custom_tool_invoker_tool
from app.agents.tools.session_history import (
    create_read_context_tool,
    create_search_context_tool,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.core.rollout_checkpoint_saver import RolloutCheckpointSaver
from app.services.business.gateway_context_query_service import (
    GatewayContextQueryService,
)
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
    workspace_root_from_response,
)
from tests.support.ports import integration_port_block_for_file
from tests.support.processes import (
    close_backend_process,
    start_backend_process,
)


def _prepare_workspace(path: Path, name: str) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    (path / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    return path


def _copy_workspace_config(source_workspace: Path, target_workspace: Path) -> None:
    target_config = target_workspace / ".boxteam" / "workspace.jsonc"
    target_config.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_workspace / ".boxteam" / "workspace.jsonc", target_config)
    shutil.copy2(
        source_workspace / ".boxteam" / "workspace_schema.jsonc",
        target_config.parent / "workspace_schema.jsonc",
    )


async def _write_session_context_checkpoint(
    *,
    workspace_root: Path,
    session_id: str,
    marker: str,
    checkpoint_id: str = "ckpt-cross-workspace-context",
) -> None:
    saver = RolloutCheckpointSaver(
        sessions_dir=workspace_root / ".boxteam" / "sessions"
    )
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(content=f"请记住 {marker}"),
                AIMessage(
                    content=[
                        {
                            "type": "reasoning",
                            "reasoning": "SECRET_REASONING_E2E",
                        },
                        {"type": "text", "text": marker},
                    ],
                    tool_calls=[
                        {
                            "name": "diagnostic_tool",
                            "args": {"value": "SECRET_TOOL_ARG_E2E"},
                            "id": "call_context_e2e",
                            "type": "tool_call",
                        }
                    ],
                ),
                ToolMessage(
                    content="SECRET_TOOL_RESULT_E2E",
                    tool_call_id="call_context_e2e",
                    name="diagnostic_tool",
                ),
            ]
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": checkpoint_id,
    }
    await saver.aput(
        build_checkpoint_config(session_id),
        checkpoint,
        {"source": "e2e_fixture", "step": 1, "writes": {}},
        {"messages": 1},
    )


class _UnexpectedLocalQueryService:
    def __getattr__(self, name: str):
        raise AssertionError(f"跨工作区工具不应调用本地查询服务: {name}")



@pytest.mark.asyncio
async def test_gateway_routes_sessions_between_local_workspaces(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
):
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(integration_workspace_root_path).resolve()
    secondary_workspace = _prepare_workspace(
        primary_workspace.parent / "secondary-workspace",
        "secondary workspace",
    )
    _copy_workspace_config(primary_workspace, secondary_workspace)

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
        refresh_config=True,
    )

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            await acquire_gateway_guest(client)
            default_workspace_response = await client.get("/api/v1/workspace")
            default_request_id = default_workspace_response.json()["request_id"]
            assert default_request_id
            assert default_workspace_response.headers["X-Request-ID"] == default_request_id
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
            assert add_response.json()["request_id"]
            assert add_response.headers["X-Request-ID"] == add_response.json()["request_id"]
            workspace_list = add_response.json()["data"]
            default_workspace_id = next(
                item["workspace_id"]
                for item in workspace_list["items"]
                if Path(item["root_path"]).resolve() == primary_workspace
            )
            assert re.fullmatch(r"gw_[0-9a-f]{32}", default_workspace_id)
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
            assert re.fullmatch(r"gw_[0-9a-f]{32}", secondary_workspace_id)
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

            async def assert_tool_capability_protocol(workspace_id: str | None) -> None:
                headers = (
                    {"X-BoxTeam-Workspace-Id": workspace_id}
                    if workspace_id is not None
                    else {}
                )
                catalog_response = await client.get(
                    "/api/v1/tools?agent_id=default",
                    headers=headers,
                )
                assert catalog_response.status_code == 200, catalog_response.text
                catalog_payload = catalog_response.json()
                assert catalog_response.headers["X-Request-ID"] == catalog_payload["request_id"]
                items = catalog_payload["data"]
                assert items
                assert all(
                    "execution_enabled" in item and "model_visible" in item
                    for item in items
                )

                patch_response = await client.patch(
                    "/api/v1/tools/selection",
                    headers=headers,
                    json={
                        "agent_id": "default",
                        "changes": [
                            {
                                "tool_id": items[0]["tool_id"],
                                "execution_enabled": items[0]["execution_enabled"],
                                "model_visible": items[0]["model_visible"],
                            }
                        ],
                    },
                )
                assert patch_response.status_code == 200, patch_response.text
                patch_payload = patch_response.json()
                assert patch_response.headers["X-Request-ID"] == patch_payload["request_id"]
                assert patch_payload["data"][0]["tool_id"] == items[0]["tool_id"]

                invalid_response = await client.patch(
                    "/api/v1/tools/selection",
                    headers=headers,
                    json={
                        "agent_id": "default",
                        "changes": [
                            {
                                "tool_id": "gateway_unknown_tool",
                                "execution_enabled": False,
                                "model_visible": False,
                            }
                        ],
                    },
                )
                assert invalid_response.status_code == 400, invalid_response.text

            await assert_tool_capability_protocol(None)
            await assert_tool_capability_protocol(secondary_workspace_id)

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


@pytest.mark.asyncio
async def test_session_context_tools_query_another_workspace_through_gateway(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
):
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(integration_workspace_root_path).resolve()
    secondary_workspace = _prepare_workspace(
        primary_workspace.parent / "context-tool-secondary-workspace",
        "context tool secondary workspace",
    )
    _copy_workspace_config(primary_workspace, secondary_workspace)
    gateway_port = port_block.port(22)
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    primary_backend = start_backend_process(
        workspace_root=str(primary_workspace),
        port=port_block.port(20),
        log_name="context-tool-primary-backend",
    )
    secondary_backend = start_backend_process(
        workspace_root=str(secondary_workspace),
        port=port_block.port(21),
        log_name="context-tool-secondary-backend",
    )
    gateway = start_gateway_process(
        workspace_root=primary_workspace,
        default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
        port=gateway_port,
    )

    try:
        async with httpx.AsyncClient(
            base_url=gateway_url,
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            await acquire_gateway_guest(client)
            add_response = await client.post(
                "/api/gateway/workspaces/local",
                json={
                    "root_path": str(secondary_workspace),
                    "name": "context-tool-secondary",
                    "backend_url": f"http://127.0.0.1:{secondary_backend.port}",
                },
            )
            assert add_response.status_code == 200, add_response.text
            workspace_items = add_response.json()["data"]["items"]
            secondary_workspace_id = next(
                item["workspace_id"]
                for item in workspace_items
                if Path(item["root_path"]).resolve() == secondary_workspace
            )

            create_response = await client.post(
                "/api/v1/sessions",
                headers={"X-BoxTeam-Workspace-Id": secondary_workspace_id},
                json={"title": "Cross Workspace Context Source"},
            )
            assert create_response.status_code == 200, create_response.text
            source_session_id = create_response.json()["data"]["session_id"]

        marker = "CROSS_WORKSPACE_CONTEXT_ALPHA"
        await _write_session_context_checkpoint(
            workspace_root=secondary_workspace,
            session_id=source_session_id,
            marker=marker,
        )

        context = SimpleNamespace(
            session_context_query_service=_UnexpectedLocalQueryService(),
            workspace_session_context_client=GatewayContextQueryService(
                transport=GatewaySessionContextClient(gateway_url=gateway_url)
            ),
        )
        read_tool = create_read_context_tool(context)
        search_tool = create_search_context_tool(context)
        resource = (
            f"boxteam://workspace/{secondary_workspace_id}/session/"
            f"{source_session_id}"
        )

        async with httpx.AsyncClient(base_url=gateway_url, timeout=30) as client:
            unauthenticated_response = await client.post(
                "/api/v1/context/read",
                headers={"X-BoxTeam-Workspace-Id": secondary_workspace_id},
                json={"resource": resource},
            )
        assert unauthenticated_response.status_code == 200, (
            unauthenticated_response.text
        )

        overview_payload = json.loads(
            await read_tool.ainvoke({"resource": resource})
        )
        revision = overview_payload["revision"]
        assert overview_payload["view"] == "overview"
        assert marker in json.dumps(overview_payload, ensure_ascii=False)
        overview_json = json.dumps(overview_payload, ensure_ascii=False)
        assert "diagnostic_tool" in overview_json
        assert "SECRET_REASONING_E2E" not in overview_json
        assert "SECRET_TOOL_ARG_E2E" not in overview_json
        assert "SECRET_TOOL_RESULT_E2E" not in overview_json

        detailed_payload = json.loads(
            await read_tool.ainvoke(
                {
                    "resource": resource,
                    "view": "records",
                    "include": [
                        "visible_text",
                        "reasoning",
                        "tool_calls",
                        "tool_results",
                    ],
                }
            )
        )
        detailed_json = json.dumps(detailed_payload, ensure_ascii=False)
        assert "SECRET_REASONING_E2E" in detailed_json
        assert "SECRET_TOOL_ARG_E2E" in detailed_json
        assert "SECRET_TOOL_RESULT_E2E" in detailed_json

        search_payload = json.loads(
            await search_tool.ainvoke(
                {
                    "resource": resource,
                    "query": marker,
                    "expected_revision": revision,
                }
            )
        )
        assert search_payload["total_matches"] == 2
        match = search_payload["matches"][0]

        read_payload = json.loads(
            await read_tool.ainvoke(
                {
                    "resource": match["locator"],
                    "view": "records",
                    "expected_revision": match["revision"],
                }
            )
        )
        assert marker in json.dumps(read_payload, ensure_ascii=False)

        inventory_payload = json.loads(
            await read_tool.ainvoke(
                {
                    "resource": "boxteam://gateway/workspaces",
                    "view": "inventory",
                }
            )
        )
        assert secondary_workspace_id in json.dumps(inventory_payload)

        gateway_search_payload = json.loads(
            await search_tool.ainvoke(
                {
                    "resource": "boxteam://gateway",
                    "query": marker,
                }
            )
        )
        assert gateway_search_payload["total_matches"] >= 2
        assert gateway_search_payload["partial_errors"] == []

        invoker = create_custom_tool_invoker_tool([read_tool, search_tool])
        await _write_session_context_checkpoint(
            workspace_root=secondary_workspace,
            session_id=source_session_id,
            marker=f"{marker}_UPDATED",
            checkpoint_id="ckpt-cross-workspace-context-updated",
        )
        stale_result = await invoker.ainvoke(
            {
                "type": "tool_call",
                "id": "call_stale_locator",
                "name": invoker.name,
                "args": {
                    "tool_name": read_tool.name,
                    "arguments": {
                        "resource": match["locator"],
                        "view": "records",
                        "expected_revision": match["revision"],
                    },
                },
            }
        )
        assert isinstance(stale_result, ToolMessage)
        assert stale_result.status == "error"
        assert "revision changed" in stale_result.text

        failed_result = await invoker.ainvoke(
            {
                "type": "tool_call",
                "id": "call_wrong_workspace",
                "name": invoker.name,
                "args": {
                    "tool_name": read_tool.name,
                    "arguments": {
                        "resource": (
                            "boxteam://workspace/gw_wrong_workspace_id/session/"
                            f"{source_session_id}"
                        )
                    },
                },
            }
        )
        assert isinstance(failed_result, ToolMessage)
        assert failed_result.status == "error"
        assert "workspace_id=gw_wrong_workspace_id" in failed_result.text

        close_backend_process(secondary_backend)
        partial_gateway_search = json.loads(
            await search_tool.ainvoke(
                {"resource": "boxteam://gateway", "query": marker}
            )
        )
        assert partial_gateway_search["partial_errors"]
    finally:
        close_gateway_process(gateway)
        close_backend_process(secondary_backend)
        close_backend_process(primary_backend)


@pytest.mark.asyncio
async def test_gateway_restores_frontend_added_managed_local_workspace(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
):
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(integration_workspace_root_path).resolve()
    managed_workspace = _prepare_workspace(
        primary_workspace.parent / "managed-local-workspace",
        "managed local workspace",
    )
    _copy_workspace_config(primary_workspace, managed_workspace)
    primary_backend = start_backend_process(
        workspace_root=str(primary_workspace),
        port=port_block.port(10),
        log_name="gateway-managed-primary-backend",
    )
    gateway = start_gateway_process(
        workspace_root=primary_workspace,
        default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
        port=port_block.port(11),
    )

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            add_response = await client.post(
                "/api/gateway/workspaces/local",
                json={"root_path": str(managed_workspace), "name": "managed-local"},
            )
            assert add_response.status_code == 200, add_response.text
            managed_item = next(
                item
                for item in add_response.json()["data"]["items"]
                if Path(item["root_path"]).resolve() == managed_workspace
            )
            managed_workspace_id = managed_item["workspace_id"]
            assert re.fullmatch(r"gw_[0-9a-f]{32}", managed_workspace_id)
            assert managed_item["managed"] is True
            activate_response = await client.post(
                f"/api/gateway/workspaces/{managed_workspace_id}/activate"
            )
            assert activate_response.status_code == 200, activate_response.text
            start_response = await client.post(
                f"/api/gateway/workspaces/{managed_workspace_id}/runtime/start"
            )
            assert start_response.status_code == 200, start_response.text

        close_gateway_process(gateway)
        gateway = start_gateway_process(
            workspace_root=primary_workspace,
            default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
            port=port_block.port(11),
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as restarted_client:
            await acquire_gateway_guest(restarted_client)
            restored_response = await restarted_client.get("/api/gateway/workspaces")
            assert restored_response.status_code == 200, restored_response.text
            restored_list = restored_response.json()["data"]
            restored_item = next(
                item
                for item in restored_list["items"]
                if item["workspace_id"] == managed_workspace_id
            )
            assert restored_item["status"] == "ready"
            assert restored_item["connection_error"] is None
            assert restored_list["active_workspace_id"] == managed_workspace_id
    finally:
        close_gateway_process(gateway)
        close_backend_process(primary_backend)
