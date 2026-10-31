"""冻结 API 适配层的边界错误映射，防止客户端输入错误泄漏成 500。

覆盖用户明确要求的「前端访问在各种边界情况下都没问题」：

- 查询/路径参数里的非法 agent_id、非法上下文 resource 必须落 4xx；
- 快照 owner 的磁盘错误必须显式失败；
- 同一工具重复启动测试的 RuntimeError 必须落 409 而不是 500。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NoReturn

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import agents as agents_api
from app.api import context as context_api
from app.api import runtime as runtime_api
from app.api import tools as tools_api
from app.api.deps import get_session_context_query_service
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.agent import WorkspaceDefaultAgentUpdateRequest
from app.schemas.internal_v2.runtime import UiSnapshotResultDTO
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextResourceDTO,
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


class _ContextBoundaryService:
    def __init__(self) -> None:
        self.error: Exception = ValueError("上下文资源 URI 不符合规范")
        self.calls = 0

    async def read_context(self, payload: SessionContextReadRequest) -> NoReturn:
        self.calls += 1
        raise self.error

    async def search_context(self, payload: SessionContextSearchRequest) -> NoReturn:
        self.calls += 1
        raise self.error


@pytest.fixture
def context_boundary_client() -> Iterator[tuple[TestClient, _ContextBoundaryService]]:
    from app.main import app as workspace_app

    service = _ContextBoundaryService()
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(context_api.router, prefix="/api/v1")
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(error_type, workspace_app.exception_handlers[error_type])
    application.dependency_overrides[get_session_context_query_service] = lambda: service
    with TestClient(application, raise_server_exceptions=False,
                    headers={"X-Request-ID": "req-context-boundary"}) as client:
        yield client, service


def _context_body(operation: str) -> dict[str, object]:
    return {"resource_identity": session_resource_identity(WORKSPACE_ID, TARGET_SESSION),
            "uri": "!!bad!!", "scope": "workspace", **({"query": "needle"} if operation == "search" else {})}


@pytest.mark.parametrize("operation", ["read", "search"])
@pytest.mark.parametrize("native", ["/private/context-response-sentinel", r"C:\private\context-response-sentinel", r"\\target\private\context-response-sentinel"])
def test_context_internal_validation_is_server_failure_without_echo(
    context_boundary_client: tuple[TestClient, _ContextBoundaryService], operation: str, native: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, service = context_boundary_client
    digest = "sha256:jcs:v1:" + "a" * 64
    with pytest.raises(ValidationError) as captured:
        SessionContextResourceDTO.model_validate({
            "display_uri": native, "resource_kind": "skills", "owner_scope": "workspace", "facet": "content",
            "revision": digest, "availability": "available", "provenance_ref": {"token": digest}, native: "unexpected",
        })
    service.error = captured.value
    response = client.post(f"/api/v1/context/{operation}", json=_context_body(operation))
    assert response.status_code == 500, response.text
    assert response.json() == {"code": 500, "message": "请求处理失败: stage=http_request, error=ValidationError",
                               "data": None, "request_id": "req-context-boundary"}
    assert response.headers["X-Request-ID"] == "req-context-boundary"
    assert "context-response-sentinel" not in response.text and "display_uri" not in response.text
    assert service.calls == 1
    # 路由不改写内部校验正文，原异常实例及动态字段位置只进入统一诊断。
    diagnostics = [record for record in caplog.records if record.exc_info and record.exc_info[1] is service.error]
    assert len(diagnostics) == 1 and "context-response-sentinel" in caplog.text


@pytest.mark.parametrize("operation", ["read", "search"])
def test_context_business_rejection_keeps_http_400_and_request_identity(
    context_boundary_client: tuple[TestClient, _ContextBoundaryService], operation: str,
) -> None:
    client, service = context_boundary_client
    response = client.post(f"/api/v1/context/{operation}", json=_context_body(operation))
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": str(service.error), "request_id": "req-context-boundary"}
    assert response.headers["X-Request-ID"] == "req-context-boundary" and service.calls == 1


@pytest.mark.parametrize("operation", ["read", "search"])
def test_context_invalid_request_is_rejected_before_service(
    context_boundary_client: tuple[TestClient, _ContextBoundaryService], operation: str,
) -> None:
    client, service = context_boundary_client
    response = client.post(f"/api/v1/context/{operation}", json={**_context_body(operation), "max_chars": 255})
    assert response.status_code == 422, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == "req-context-boundary"
    assert response.json()["detail"][0]["loc"] == ["body", "max_chars"]
    assert service.calls == 0


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
