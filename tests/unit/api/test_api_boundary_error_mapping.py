"""冻结 API 适配层的边界错误映射，防止客户端输入错误泄漏成 500。

覆盖用户明确要求的「前端访问在各种边界情况下都没问题」：

- 查询/路径参数里的非法 agent_id、非法上下文 resource 必须落 4xx；
- 快照 owner 的磁盘错误必须显式失败；
- 同一工具重复启动测试的 RuntimeError 必须落 409 而不是 500。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import agents as agents_api
from app.api import context as context_api
from app.api import runtime as runtime_api
from app.api import tools as tools_api
from app.schemas.internal_v2.agent import WorkspaceDefaultAgentUpdateRequest
from app.schemas.internal_v2.runtime import UiSnapshotResultDTO
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextSearchRequest,
)
from app.schemas.internal_v2.tool import ToolSelectionPatchRequest
from app.schemas.internal_v2.tool_test import ToolTestRunDTO
from app.services.business.session_context_resource import session_resource_identity
from tests.support.communication.probe import TARGET_SESSION, WORKSPACE_ID


class _MissingAgentToolService:
    """ToolService.list 在 agent 不存在时抛 ValueError（配置层口径）。"""

    async def list(self, agent_id: str = "default"):
        raise ValueError(f"agent {agent_id} 不存在")

    async def get(self, tool_id: str, agent_id: str = "default"):
        raise ValueError(f"agent {agent_id} 不存在")


class _BusyToolTestService:
    async def start(self, *, tool_name: str, request):
        raise RuntimeError(f"工具测试正在运行，不能重复启动: {tool_name}")


class _UnsupportedToolTestService:
    async def start(self, *, tool_name: str, request):
        raise ValueError(f"工具尚未提供模型调用测试: {tool_name}")


class _RejectingContextService:
    async def search_context(self, payload):
        raise ValueError("上下文资源 URI 不符合规范")

    async def read_context(self, payload):
        raise ValueError("上下文资源 URI 不符合规范")


class _UnwritableLogService:
    async def write_html_snapshot(self, record):
        raise PermissionError("快照 owner 目录不可写")


class _RejectingAgentService:
    async def set_workspace_default_agent(self, agent_id: str):
        raise ValueError(f"agent {agent_id} 不存在")


@pytest.mark.asyncio
async def test_list_tools_maps_unknown_agent_to_400() -> None:
    with pytest.raises(HTTPException) as captured:
        await tools_api.list_tools(
            "nope",
            _="local",
            request_id="req",
            tool_service=_MissingAgentToolService(),
        )

    assert captured.value.status_code == 400


@pytest.mark.asyncio
async def test_get_tool_maps_unknown_agent_to_400() -> None:
    with pytest.raises(HTTPException) as captured:
        await tools_api.get_tool(
            "edit_file",
            "nope",
            _="local",
            request_id="req",
            tool_service=_MissingAgentToolService(),
        )

    assert captured.value.status_code == 400


@pytest.mark.asyncio
async def test_start_tool_test_maps_unsupported_tool_to_400() -> None:
    with pytest.raises(HTTPException) as captured:
        await tools_api.start_tool_test(
            "unknown_tool",
            payload=_start_request(),
            _="local",
            request_id="req",
            test_service=_UnsupportedToolTestService(),
        )

    assert captured.value.status_code == 400


@pytest.mark.asyncio
async def test_start_tool_test_maps_busy_tool_to_409() -> None:
    with pytest.raises(HTTPException) as captured:
        await tools_api.start_tool_test(
            "edit_file",
            payload=_start_request(),
            _="local",
            request_id="req",
            test_service=_BusyToolTestService(),
        )

    assert captured.value.status_code == 409


@pytest.mark.asyncio
async def test_search_context_maps_bad_resource_to_400() -> None:
    with pytest.raises(HTTPException) as captured:
        await context_api.search_context(
            SessionContextSearchRequest(
                resource_identity=session_resource_identity(WORKSPACE_ID, TARGET_SESSION),
                uri="!!bad!!", scope="workspace", query="x",
            ),
            request_id="req",
            query_service=_RejectingContextService(),
        )

    assert captured.value.status_code == 400


@pytest.mark.asyncio
async def test_read_context_maps_bad_resource_to_400() -> None:
    with pytest.raises(HTTPException) as captured:
        await context_api.read_context(
            _read_request(),
            request_id="req",
            query_service=_RejectingContextService(),
        )

    assert captured.value.status_code == 400


@pytest.mark.asyncio
async def test_save_log_snapshot_exposes_owner_storage_failure() -> None:
    with pytest.raises(PermissionError, match="owner 目录不可写"):
        await runtime_api.save_log_snapshot(
            payload=runtime_api.UiSnapshotRequest(
                html="<p>x</p>",
            ),
            _="local",
            request_id="req",
            log_service=_UnwritableLogService(),
        )


@pytest.mark.asyncio
async def test_set_workspace_default_agent_maps_unknown_agent_to_404() -> None:
    with pytest.raises(HTTPException) as captured:
        await agents_api.set_workspace_default_agent(
            WorkspaceDefaultAgentUpdateRequest(agent_id="nope"),
            _="local",
            request_id="req",
            agent_service=_RejectingAgentService(),
        )

    assert captured.value.status_code == 404


def _start_request():
    from app.schemas.internal_v2.tool_test import ToolTestStartRequest

    return ToolTestStartRequest(agent_id="default", provider_ids=[], repetitions=1)


def _read_request():
    return SessionContextReadRequest(
        resource_identity=session_resource_identity(WORKSPACE_ID, TARGET_SESSION),
        uri="!!bad!!", scope="workspace", view="overview",
    )


# 显式引用，避免未使用 import 被误删后失去契约覆盖。
_KEPT_TYPES = (UiSnapshotResultDTO, ToolTestRunDTO, ToolSelectionPatchRequest)
