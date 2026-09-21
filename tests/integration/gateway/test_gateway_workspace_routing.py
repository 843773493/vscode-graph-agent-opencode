from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.tools.custom_invocation import (
    create_extension_tool_invoker_tool,
    seal_extension_catalog_binding_from_tools,
)
from app.agents.tools.session_history import (
    create_read_context_tool,
    create_search_context_tool,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.federation import (
    FEDERATION_PROTOCOL_VERSION,
    RemoteGatewayConnection,
)
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.remote_gateway import refresh_remote_gateway_projections
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.services.business.gateway_context_query_service import (
    GatewayContextQueryService,
)
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
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


def _remove_remote_gateway_registration(
    *,
    registry: GatewayWorkspaceRegistry | None,
    gateway_root: Path,
    connection_id: str,
) -> None:
    """移除测试注册的远程 Gateway 投影与连接，避免残留污染共享 BOXTEAM_HOME。

    远程投影工作区引用连接；连接定义由测试直接写入 registry，不会随用户配置
    重建。若删除投影时留下孤儿投影，下一次 Gateway 启动会按 fail-closed 拒绝
    加载（引用未知连接），因此测试必须在退出时把两者一起清理掉。
    """

    if registry is not None:
        for target in registry.targets():
            if target.remote_gateway_connection_id == connection_id:
                # owner="registry" 让这条清理写入 registry 自身的完整快照，
                # 从而把已无人引用的连接从持久化元数据里一并移除。
                registry.remove(target.workspace_id, owner="registry")
    FederationCredentialStore(
        storage_path=gateway_root / "credentials" / "federation.json"
    ).remove(connection_id)


@pytest.fixture(autouse=True)
def isolated_gateway_state(
    integration_workspace_root_path: str,
) -> None:
    """每个测试函数都从干净的 Gateway 持久状态开始。

    同一个测试内会多次调用 `start_gateway_process` 重启 Gateway，并依赖
    `state/gateway` 中的持久化状态（pending 恢复、stale pending 等），因此
    这里只在测试函数开始前重置；测试函数内部产生的状态仍然跨进程保留。
    上一次运行或上一个用例遗留的远程投影、连接和 pending 候选不会让后续
    用例启动失败或读到过期 pending。
    """

    workspace_root = Path(integration_workspace_root_path)
    # 本测试文件同时使用主工作区与 remote-gateway-host 下的远程工作区，两者各自
    # 带一份跨运行持久的 BOXTEAM_HOME，都要从干净状态开始。
    for root in (
        workspace_root,
        workspace_root.parent / "remote-gateway-host" / "workspace",
    ):
        reset_gateway_persistent_state(workspace_root=root)


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
        "channel_versions": {"messages": "1"},
        "updated_channels": ["messages"],
        "id": checkpoint_id,
    }
    await saver.aput(
        build_checkpoint_config(session_id),
        checkpoint,
        {"source": "e2e_fixture", "step": 1, "writes": {}},
        {"messages": "1"},
    )


async def _wait_for_gateway_pending_restart(
    client: httpx.AsyncClient,
) -> dict[str, object]:
    deadline = time.monotonic() + 60
    last_data: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = await client.get("/api/gateway/config/reload-status")
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert isinstance(data, dict)
        last_data = data
        if data.get("state") == "pending_restart" and data.get("candidate_ref"):
            return data
        await asyncio.sleep(0.25)
    raise AssertionError(
        "Gateway 未在 60 秒内生成 pending restart: "
        f"{last_data}"
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

        invoker = create_extension_tool_invoker_tool(
            [read_tool, search_tool],
            catalog_binding_resolver=seal_extension_catalog_binding_from_tools(
                [read_tool, search_tool]
            ),
        )
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
            existing_response = await client.get("/api/gateway/workspaces")
            assert existing_response.status_code == 200, existing_response.text
            for existing_item in existing_response.json()["data"]["items"]:
                if Path(existing_item["root_path"]).resolve() != managed_workspace:
                    continue
                cleanup_response = await client.delete(
                    f"/api/gateway/workspaces/{existing_item['workspace_id']}"
                )
                assert cleanup_response.status_code == 200, cleanup_response.text
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
            cleanup_response = await restarted_client.delete(
                f"/api/gateway/workspaces/{managed_workspace_id}"
            )
            assert cleanup_response.status_code == 200, cleanup_response.text
    finally:
        close_gateway_process(gateway)
        close_backend_process(primary_backend)


@pytest.mark.asyncio
async def test_gateway_pending_restart_is_loaded_by_new_gateway_process(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
):
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(integration_workspace_root_path).resolve()
    primary_backend = start_backend_process(
        workspace_root=str(primary_workspace),
        port=port_block.port(10),
        log_name="gateway-pending-primary-backend",
    )
    gateway = start_gateway_process(
        workspace_root=primary_workspace,
        default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
        port=port_block.port(11),
    )
    gateway_config_path = (
        primary_workspace.parent / "boxteam-home" / "config" / "gateway.jsonc"
    )
    gateway_state_path = (
        primary_workspace.parent
        / "boxteam-home"
        / "state"
        / "gateway"
        / "gateway.sqlite"
    )

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            document = gateway_config_path.read_text(encoding="utf-8")
            changed_document = document.replace(
                '"poll_interval_seconds": 0.5',
                '"poll_interval_seconds": 0.75',
                1,
            )
            assert changed_document != document
            gateway_config_path.write_text(changed_document, encoding="utf-8")
            pending_status = await _wait_for_gateway_pending_restart(client)

        close_gateway_process(gateway)
        gateway = None
        state = GatewayStateStore(path=gateway_state_path)
        try:
            candidate_ref = pending_status["candidate_ref"]
            assert isinstance(candidate_ref, str)
            intent = state.get_gateway_restart_intent(candidate_ref=candidate_ref)
            assert intent is not None
            pending = state.get_pending_config_candidate(
                config_domain="gateway",
                candidate_id=intent.candidate_id,
            )
            assert pending is not None
        finally:
            state.close()

        gateway = start_gateway_process(
            workspace_root=primary_workspace,
            default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
            port=port_block.port(11),
            extra_env={
                "BOXTEAM_CONFIG_CANDIDATE_REF": intent.candidate_ref,
                "BOXTEAM_CONFIG_GENERATION": intent.target_generation,
                "BOXTEAM_CONFIG_FENCING_TOKEN": intent.fencing_token,
            },
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as restarted_client:
            await acquire_gateway_guest(restarted_client)
            status_response = await restarted_client.get(
                "/api/gateway/config/reload-status"
            )
            assert status_response.status_code == 200, status_response.text
            status = status_response.json()["data"]
            assert status["state"] == "active"
            assert status["restart_required"] is False
            assert status["candidate_ref"] is None
            assert status["pending_revision"] is None
            assert status["candidate_id"] is None
            assert status["attempt_id"] is None
            assert status["apply_id"] is None

        close_gateway_process(gateway)
        gateway = None
        state = GatewayStateStore(path=gateway_state_path)
        try:
            active_snapshot = state.get_active_config_snapshot("gateway")
            assert active_snapshot is not None
            assert (
                active_snapshot.payload["runtime"]["gateway"]["process"]["health"][
                    "poll_interval_seconds"
                ]
                == 0.75
            )
            new_generation = state.get_gateway_runtime_generation(
                generation_id=intent.target_generation
            )
            old_generation = state.get_gateway_runtime_generation(
                generation_id=intent.old_generation
            )
            assert new_generation is not None
            assert new_generation.state == "closed"
            assert new_generation.listener_state == "closed"
            assert old_generation is not None
            assert old_generation.state == "active"
            assert old_generation.listener_state == "draining"
        finally:
            state.close()
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        close_backend_process(primary_backend)


@pytest.mark.asyncio
async def test_gateway_expired_pending_requires_explicit_retry_before_startup(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
):
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(integration_workspace_root_path).resolve()
    primary_backend = start_backend_process(
        workspace_root=str(primary_workspace),
        port=port_block.port(12),
        log_name="gateway-expired-pending-backend",
    )
    gateway = start_gateway_process(
        workspace_root=primary_workspace,
        default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
        port=port_block.port(13),
    )
    gateway_config_path = (
        primary_workspace.parent / "boxteam-home" / "config" / "gateway.jsonc"
    )
    gateway_state_path = (
        primary_workspace.parent
        / "boxteam-home"
        / "state"
        / "gateway"
        / "gateway.sqlite"
    )

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            document = gateway_config_path.read_text(encoding="utf-8")
            changed_document = document
            for old_value in (0.5, 0.75, 0.8):
                changed_document = document.replace(
                    f'"poll_interval_seconds": {old_value}',
                    '"poll_interval_seconds": 0.85',
                    1,
                )
                if changed_document != document:
                    break
            assert changed_document != document
            gateway_config_path.write_text(changed_document, encoding="utf-8")
            pending_status = await _wait_for_gateway_pending_restart(client)

        close_gateway_process(gateway)
        gateway = None
        state = GatewayStateStore(path=gateway_state_path)
        try:
            candidate_ref = pending_status["candidate_ref"]
            assert isinstance(candidate_ref, str)
            intent = state.get_gateway_restart_intent(candidate_ref=candidate_ref)
            assert intent is not None
            with state.connection() as connection:
                connection.execute(
                    "UPDATE gateway_restart_intent SET expires_at = ? "
                    "WHERE candidate_ref = ?",
                    ("1970-01-01T00:00:00+00:00", candidate_ref),
                )
                connection.commit()
        finally:
            state.close()

        with pytest.raises(RuntimeError, match="Gateway 提前退出"):
            start_gateway_process(
                workspace_root=primary_workspace,
                default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
                port=port_block.port(13),
                extra_env={
                    "BOXTEAM_CONFIG_CANDIDATE_REF": intent.candidate_ref,
                    "BOXTEAM_CONFIG_GENERATION": intent.target_generation,
                    "BOXTEAM_CONFIG_FENCING_TOKEN": intent.fencing_token,
                },
            )

        state = GatewayStateStore(path=gateway_state_path)
        try:
            retried = state.retry_gateway_restart(
                candidate_ref=intent.candidate_ref,
                target_generation="gateway-generation-expired-retry",
                requested_by="integration-test-retry",
            )
        finally:
            state.close()

        gateway = start_gateway_process(
            workspace_root=primary_workspace,
            default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
            port=port_block.port(13),
            extra_env={
                "BOXTEAM_CONFIG_CANDIDATE_REF": retried.candidate_ref,
                "BOXTEAM_CONFIG_GENERATION": retried.target_generation,
                "BOXTEAM_CONFIG_FENCING_TOKEN": retried.fencing_token,
            },
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            status_response = await client.get("/api/gateway/config/reload-status")
            assert status_response.status_code == 200, status_response.text
            status = status_response.json()["data"]
            assert status["state"] == "active"
            assert status["pending_revision"] is None
            assert status["candidate_id"] is None
            assert status["candidate_ref"] is None
            assert status["attempt_id"] is None
            assert status["apply_id"] is None
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        close_backend_process(primary_backend)


@pytest.mark.asyncio
async def test_gateway_pending_startup_failure_keeps_active_snapshot_recoverable(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
):
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(integration_workspace_root_path).resolve()
    primary_backend = start_backend_process(
        workspace_root=str(primary_workspace),
        port=port_block.port(14),
        log_name="gateway-pending-startup-failure-backend",
    )
    gateway = start_gateway_process(
        workspace_root=primary_workspace,
        default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
        port=port_block.port(15),
    )
    gateway_config_path = (
        primary_workspace.parent / "boxteam-home" / "config" / "gateway.jsonc"
    )
    gateway_state_path = (
        primary_workspace.parent
        / "boxteam-home"
        / "state"
        / "gateway"
        / "gateway.sqlite"
    )

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            document = gateway_config_path.read_text(encoding="utf-8")
            changed_document = document.replace(
                '"poll_interval_seconds": 0.5',
                '"poll_interval_seconds": 0.9',
                1,
            )
            assert changed_document != document
            gateway_config_path.write_text(changed_document, encoding="utf-8")
            pending_status = await _wait_for_gateway_pending_restart(client)

        close_gateway_process(gateway)
        gateway = None
        state = GatewayStateStore(path=gateway_state_path)
        try:
            candidate_ref = pending_status["candidate_ref"]
            assert isinstance(candidate_ref, str)
            intent = state.get_gateway_restart_intent(candidate_ref=candidate_ref)
            assert intent is not None
            pending = state.get_pending_config_candidate(
                config_domain="gateway",
                candidate_id=intent.candidate_id,
            )
            assert pending is not None
            active_before = state.get_active_config_snapshot("gateway")
            assert active_before is not None
            active_poll_interval = active_before.payload["runtime"]["gateway"][
                "process"
            ]["health"]["poll_interval_seconds"]
            with state.connection() as connection:
                connection.execute(
                    "UPDATE config_pending_candidate SET payload_json = ? "
                    "WHERE config_domain = 'gateway' AND candidate_id = ?",
                    (
                        json.dumps(
                            {"runtime": {"gateway": {"process": "invalid"}}},
                            ensure_ascii=False,
                        ),
                        pending.candidate_id,
                    ),
                )
                connection.commit()
        finally:
            state.close()

        with pytest.raises(RuntimeError, match="Gateway 提前退出"):
            start_gateway_process(
                workspace_root=primary_workspace,
                default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
                port=port_block.port(15),
                extra_env={
                    "BOXTEAM_CONFIG_CANDIDATE_REF": intent.candidate_ref,
                    "BOXTEAM_CONFIG_GENERATION": intent.target_generation,
                    "BOXTEAM_CONFIG_FENCING_TOKEN": intent.fencing_token,
                },
            )

        state = GatewayStateStore(path=gateway_state_path)
        try:
            failed_intent = state.get_gateway_restart_intent(
                candidate_ref=intent.candidate_ref
            )
            failed_pending = state.get_pending_config_candidate(
                config_domain="gateway",
                candidate_id=intent.candidate_id,
            )
            active_snapshot = state.get_active_config_snapshot("gateway")
            assert failed_intent is not None
            assert failed_intent.state == "recovery_required"
            assert failed_intent.last_error is not None
            assert failed_pending is not None
            assert failed_pending.state == "recovery_required"
            assert active_snapshot is not None
            assert active_snapshot.state == "active"
            assert (
                active_snapshot.payload["runtime"]["gateway"]["process"]["health"][
                    "poll_interval_seconds"
                ]
                == active_poll_interval
            )
        finally:
            state.close()

        gateway = start_gateway_process(
            workspace_root=primary_workspace,
            default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
            port=port_block.port(15),
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as recovered_client:
            await acquire_gateway_guest(recovered_client)
            status_response = await recovered_client.get(
                "/api/gateway/config/reload-status"
            )
            assert status_response.status_code == 200, status_response.text
            status = status_response.json()["data"]
            assert status["state"] == "recovery_required"
            assert status["restart_required"] is False
            assert status["candidate_ref"] == intent.candidate_ref
            assert status["pending_revision"] == pending.pending_revision
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        close_backend_process(primary_backend)


@pytest.mark.asyncio
async def test_gateway_stale_pending_startup_cannot_mutate_restart_state(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
):
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(integration_workspace_root_path).resolve()
    primary_backend = start_backend_process(
        workspace_root=str(primary_workspace),
        port=port_block.port(16),
        log_name="gateway-stale-pending-backend",
    )
    gateway = start_gateway_process(
        workspace_root=primary_workspace,
        default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
        port=port_block.port(17),
    )
    gateway_config_path = (
        primary_workspace.parent / "boxteam-home" / "config" / "gateway.jsonc"
    )
    gateway_state_path = (
        primary_workspace.parent
        / "boxteam-home"
        / "state"
        / "gateway"
        / "gateway.sqlite"
    )

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            document = gateway_config_path.read_text(encoding="utf-8")
            changed_document = document.replace(
                '"poll_interval_seconds": 0.5',
                '"poll_interval_seconds": 0.95',
                1,
            )
            assert changed_document != document
            gateway_config_path.write_text(changed_document, encoding="utf-8")
            pending_status = await _wait_for_gateway_pending_restart(client)

        close_gateway_process(gateway)
        gateway = None
        state = GatewayStateStore(path=gateway_state_path)
        try:
            candidate_ref = pending_status["candidate_ref"]
            assert isinstance(candidate_ref, str)
            intent = state.get_gateway_restart_intent(candidate_ref=candidate_ref)
            assert intent is not None
            pending = state.get_pending_config_candidate(
                config_domain="gateway",
                candidate_id=intent.candidate_id,
            )
            assert pending is not None
            pending_last_error = pending.last_error
        finally:
            state.close()

        with pytest.raises(RuntimeError, match="Gateway 提前退出"):
            start_gateway_process(
                workspace_root=primary_workspace,
                default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
                port=port_block.port(17),
                extra_env={
                    "BOXTEAM_CONFIG_CANDIDATE_REF": intent.candidate_ref,
                    "BOXTEAM_CONFIG_GENERATION": (
                        f"stale-{intent.target_generation}"
                    ),
                    "BOXTEAM_CONFIG_FENCING_TOKEN": intent.fencing_token,
                },
            )

        state = GatewayStateStore(path=gateway_state_path)
        try:
            unchanged_intent = state.get_gateway_restart_intent(
                candidate_ref=intent.candidate_ref
            )
            unchanged_pending = state.get_pending_config_candidate(
                config_domain="gateway",
                candidate_id=intent.candidate_id,
            )
            assert unchanged_intent is not None
            assert unchanged_intent.state == "pending"
            assert unchanged_intent.last_error is None
            assert unchanged_pending is not None
            assert unchanged_pending.state == "pending_restart"
            assert unchanged_pending.last_error == pending_last_error
        finally:
            state.close()

        gateway = start_gateway_process(
            workspace_root=primary_workspace,
            default_backend_url=f"http://127.0.0.1:{primary_backend.port}",
            port=port_block.port(17),
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as recovered_client:
            await acquire_gateway_guest(recovered_client)
            status_response = await recovered_client.get(
                "/api/gateway/config/reload-status"
            )
            assert status_response.status_code == 200, status_response.text
            status = status_response.json()["data"]
            assert status["state"] == "pending_restart"
            assert status["restart_required"] is True
            assert status["candidate_ref"] == intent.candidate_ref
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        close_backend_process(primary_backend)


@pytest.mark.asyncio
async def test_gateway_federation_reconciles_pending_restart_offline_and_cursor_gap(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    monkeypatch: pytest.MonkeyPatch,
):
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    local_workspace = Path(integration_workspace_root_path).resolve()
    remote_workspace = _prepare_workspace(
        local_workspace.parent / "remote-gateway-host" / "workspace",
        "remote gateway workspace",
    )
    _copy_workspace_config(local_workspace, remote_workspace)
    remote_backend = start_backend_process(
        workspace_root=str(remote_workspace),
        port=port_block.port(30),
        log_name="gateway-federation-remote-backend",
    )
    remote_gateway = start_gateway_process(
        workspace_root=remote_workspace,
        default_backend_url=f"http://127.0.0.1:{remote_backend.port}",
        port=port_block.port(31),
    )
    local_gateway_root = (
        local_workspace.parent / "boxteam-home" / "state" / "gateway"
    )
    remote_gateway_root = (
        remote_workspace.parent / "boxteam-home" / "state" / "gateway"
    )
    local_state = GatewayStateStore(path=local_gateway_root / "gateway.sqlite")
    local_registry: GatewayWorkspaceRegistry | None = None
    connection_id = "rgw_real_process_federation"
    try:
        monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(local_gateway_root))
        remote_gateway_id = load_or_create_gateway_id(
            remote_gateway_root / "identity.json"
        )
        credential = FederationCredentialStore(
            storage_path=local_gateway_root / "credentials" / "federation.json"
        ).issue(
            connection_id=connection_id,
            peer_gateway_id="gateway_local_federation_test",
        )
        FederationCredentialStore(
            storage_path=remote_gateway_root / "credentials" / "federation.json"
        ).put(credential)
        local_registry = GatewayWorkspaceRegistry(
            storage_path=local_gateway_root / "workspaces.json",
            state_store=local_state,
        )
        local_registry.upsert_remote_gateway(
            RemoteGatewayConnection(
                connection_id=connection_id,
                name="real remote gateway",
                host="127.0.0.1",
                port=0,
                username="integration",
                private_key_path=None,
                ssh_config_host=None,
                remote_gateway_port=remote_gateway.port,
                remote_gateway_id=remote_gateway_id,
                protocol_version=FEDERATION_PROTOCOL_VERSION,
                source_owner="manual",
            ),
            runtime=WorkspaceRuntime(
                service_urls={
                    "workspace_api": f"http://127.0.0.1:{remote_gateway.port}"
                }
            ),
        )

        projected = await refresh_remote_gateway_projections(
            registry=local_registry,
            connection_id=connection_id,
        )
        assert projected
        projected_workspace_id = projected[0].workspace_id
        initial_cursor = local_registry.remote_gateway_connection(
            connection_id
        ).remote_config_event_cursor

        remote_config_path = (
            remote_workspace.parent / "boxteam-home" / "config" / "gateway.jsonc"
        )
        document = remote_config_path.read_text(encoding="utf-8")
        changed_document = document.replace(
            '"poll_interval_seconds": 0.5',
            '"poll_interval_seconds": 0.75',
            1,
        )
        assert changed_document != document
        remote_config_path.write_text(changed_document, encoding="utf-8")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{remote_gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as remote_client:
            remote_pending_status = await _wait_for_gateway_pending_restart(
                remote_client
            )
        refreshed_pending = await refresh_remote_gateway_projections(
            registry=local_registry,
            connection_id=connection_id,
        )
        assert refreshed_pending[0].workspace_id == projected_workspace_id
        pending_connection = local_registry.remote_gateway_connection(connection_id)
        assert pending_connection.remote_restart_required is True
        assert pending_connection.remote_candidate_ref == remote_pending_status[
            "candidate_ref"
        ]
        assert pending_connection.remote_config_event_cursor is not None
        assert pending_connection.remote_config_event_cursor > (initial_cursor or 0)

        close_gateway_process(remote_gateway)
        remote_gateway = None
        with pytest.raises(httpx.HTTPError):
            await refresh_remote_gateway_projections(
                registry=local_registry,
                connection_id=connection_id,
            )
        assert local_registry.has_target(projected_workspace_id)
        assert (
            local_registry.remote_gateway_connection(connection_id).remote_candidate_ref
            == remote_pending_status["candidate_ref"]
        )

        remote_state = GatewayStateStore(path=remote_gateway_root / "gateway.sqlite")
        try:
            candidate_ref = remote_pending_status["candidate_ref"]
            assert isinstance(candidate_ref, str)
            intent = remote_state.get_gateway_restart_intent(
                candidate_ref=candidate_ref
            )
            assert intent is not None
            remote_first_cursor, remote_max_cursor = remote_state.config_event_bounds(
                config_domain="gateway"
            )
            assert remote_first_cursor is not None
            assert remote_max_cursor >= 1
        finally:
            remote_state.close()

        remote_gateway = start_gateway_process(
            workspace_root=remote_workspace,
            default_backend_url=f"http://127.0.0.1:{remote_backend.port}",
            port=port_block.port(31),
            extra_env={
                "BOXTEAM_CONFIG_CANDIDATE_REF": intent.candidate_ref,
                "BOXTEAM_CONFIG_GENERATION": intent.target_generation,
                "BOXTEAM_CONFIG_FENCING_TOKEN": intent.fencing_token,
            },
        )
        refreshed_active = await refresh_remote_gateway_projections(
            registry=local_registry,
            connection_id=connection_id,
        )
        assert refreshed_active[0].workspace_id == projected_workspace_id
        active_connection = local_registry.remote_gateway_connection(connection_id)
        assert active_connection.remote_restart_required is False
        assert active_connection.remote_candidate_ref is None

        remote_managed_root = remote_workspace.parent / "managed-projection"
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{remote_gateway.port}",
            headers={"X-BoxTeam-Federation-Token": credential.token},
            timeout=60,
        ) as remote_client:
            create_response = await remote_client.post(
                "/api/gateway/federation/managed-workspaces",
                json={
                    "root_path": str(remote_managed_root),
                    "name": "projected managed workspace",
                    "create_directory": True,
                },
            )
            assert create_response.status_code == 200, create_response.text
        projected_after_update = await refresh_remote_gateway_projections(
            registry=local_registry,
            connection_id=connection_id,
        )
        assert any(
            item.root_path == str(remote_managed_root.resolve())
            for item in projected_after_update
        )

        second_document = remote_config_path.read_text(encoding="utf-8")
        second_changed_document = second_document.replace(
            '"poll_interval_seconds": 0.75',
            '"poll_interval_seconds": 0.8',
            1,
        )
        assert second_changed_document != second_document
        remote_config_path.write_text(second_changed_document, encoding="utf-8")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{remote_gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as remote_client:
            await _wait_for_gateway_pending_restart(remote_client)
        close_gateway_process(remote_gateway)
        remote_gateway = None
        remote_state = GatewayStateStore(path=remote_gateway_root / "gateway.sqlite")
        try:
            _, max_cursor = remote_state.config_event_bounds(config_domain="gateway")
            assert max_cursor >= 3
            connection = remote_state.connection()
            try:
                connection.execute(
                    """
                    UPDATE config_events
                    SET occurred_at = '2000-01-01T00:00:00+00:00'
                    WHERE config_domain = 'gateway' AND event_seq < ?
                    """,
                    (max_cursor,),
                )
                connection.commit()
            finally:
                connection.close()
            assert remote_state.prune_config_events(config_domain="gateway") >= 1
            first_after_prune, _ = remote_state.config_event_bounds(
                config_domain="gateway"
            )
            assert first_after_prune == max_cursor
        finally:
            remote_state.close()
        remote_gateway = start_gateway_process(
            workspace_root=remote_workspace,
            default_backend_url=f"http://127.0.0.1:{remote_backend.port}",
            port=port_block.port(31),
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{remote_gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as remote_client:
            gap_response = await remote_client.get(
                "/api/gateway/config/events",
                params={"after": max_cursor - 2},
            )
            assert gap_response.status_code == 410, gap_response.text
            assert gap_response.json()["detail"]["code"] == "snapshot_required"
    finally:
        _remove_remote_gateway_registration(
            registry=local_registry,
            gateway_root=local_gateway_root,
            connection_id=connection_id,
        )
        if local_registry is not None:
            local_registry.close()
        local_state.close()
        if remote_gateway is not None:
            close_gateway_process(remote_gateway)
        close_backend_process(remote_backend)
