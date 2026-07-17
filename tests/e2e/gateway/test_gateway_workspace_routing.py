from __future__ import annotations

import shutil
import re
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.tools.custom_invocation import create_custom_tool_invoker_tool
from app.agents.tools.session_history import (
    create_grep_session_context_jsonl_tool,
    create_read_session_context_jsonl_tool,
    create_read_session_recent_text_messages_tool,
)
from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
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


def _copy_workspace_config(source_workspace: Path, target_workspace: Path) -> None:
    target_config = target_workspace / ".boxteam" / "boxteam.jsonc"
    target_config.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_workspace / ".boxteam" / "boxteam.jsonc", target_config)
    shutil.copy2(
        source_workspace / ".boxteam" / "config.schema.jsonc",
        target_config.parent / "config.schema.jsonc",
    )


async def _write_session_context_checkpoint(
    *,
    workspace_root: Path,
    session_id: str,
    marker: str,
) -> None:
    saver = FileSystemCheckpointSaver(
        sessions_dir=workspace_root / ".boxteam" / "sessions"
    )
    checkpoint = {
        "channel_values": {
            "messages": [
                HumanMessage(content=f"请记住 {marker}"),
                AIMessage(content=[{"type": "text", "text": marker}]),
            ]
        },
        "channel_versions": {"messages": 1},
        "updated_channels": ["messages"],
        "id": "ckpt-cross-workspace-context",
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
    e2e_workspace_root_path: str,
):
    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(e2e_workspace_root_path).resolve()
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
    )

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
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
    e2e_workspace_root_path: str,
):
    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(e2e_workspace_root_path).resolve()
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
        env_overrides={"BOXTEAM_GATEWAY_URL": gateway_url},
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
            workspace_session_context_client=GatewaySessionContextClient(
                gateway_url=gateway_url
            ),
        )
        recent_tool = create_read_session_recent_text_messages_tool(context)
        grep_tool = create_grep_session_context_jsonl_tool(context)
        read_tool = create_read_session_context_jsonl_tool(context)

        recent = await recent_tool.ainvoke(
            {
                "workspace_id": secondary_workspace_id,
                "session_id": source_session_id,
            }
        )
        recent_payload = json.loads(recent)
        snapshot_id = recent_payload["context_snapshot"]["snapshot_id"]
        assert recent_payload["session_id"] == source_session_id
        assert marker in recent

        grep_result = await grep_tool.ainvoke(
            {
                "workspace_id": secondary_workspace_id,
                "session_id": source_session_id,
                "pattern": marker,
                "expected_snapshot_id": snapshot_id,
            }
        )
        grep_payload = json.loads(grep_result)
        assert grep_payload["context_snapshot"]["consistency"] == "matched"
        assert grep_payload["returned_match_count"] == 2

        read_result = await read_tool.ainvoke(
            {
                "workspace_id": secondary_workspace_id,
                "session_id": source_session_id,
                "line_start": grep_payload["matches"][0]["line_number"],
                "line_count": 1,
                "expected_snapshot_id": snapshot_id,
            }
        )
        read_payload = json.loads(read_result)
        assert read_payload["context_snapshot"]["consistency"] == "matched"
        assert marker in read_payload["lines"][0]["text"]

        invoker = create_custom_tool_invoker_tool([recent_tool, grep_tool, read_tool])
        failed_result = await invoker.ainvoke(
            {
                "type": "tool_call",
                "id": "call_wrong_workspace",
                "name": invoker.name,
                "args": {
                    "tool_name": recent_tool.name,
                    "arguments": {
                        "workspace_id": "gw_wrong_workspace_id",
                        "session_id": source_session_id,
                    },
                },
            }
        )
        assert isinstance(failed_result, ToolMessage)
        assert failed_result.status == "error"
        assert "workspace_id=gw_wrong_workspace_id" in failed_result.text
        assert "修正 workspace_id 或 session_id 后重试" in failed_result.text
        assert "提醒用户" in failed_result.text
    finally:
        close_gateway_process(gateway)
        close_backend_process(secondary_backend)
        close_backend_process(primary_backend)


@pytest.mark.asyncio
async def test_gateway_restores_frontend_added_managed_local_workspace(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
):
    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    primary_workspace = Path(e2e_workspace_root_path).resolve()
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
