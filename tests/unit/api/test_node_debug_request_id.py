from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from app.api.deps import get_node_debug_service, workspace_container_scope
from app.core.path_utils import workspace_context
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.main import app
from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId
from app.services.infrastructure.node_debug.service import NodeDebugService

_SESSION_ID = "ses_00000000400070008000000000000001"
_WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def node_debug_asgi_client(
    tmp_path: Path,
) -> Generator[tuple[TestClient, MagicMock], None, None]:
    node_debug_service = MagicMock(spec=NodeDebugService)

    async def bind_workspace() -> AsyncIterator[None]:
        with workspace_context(_WORKSPACE_ID, tmp_path):
            yield

    app.dependency_overrides[get_node_debug_service] = lambda: node_debug_service
    app.dependency_overrides[workspace_container_scope] = bind_workspace
    try:
        yield (
            TestClient(
                app,
                raise_server_exceptions=False,
                headers={"X-BoxTeam-Workspace-Id": _WORKSPACE_ID},
            ),
            node_debug_service,
        )
    finally:
        app.dependency_overrides.pop(get_node_debug_service, None)
        app.dependency_overrides.pop(workspace_container_scope, None)


def test_node_debug_http_error_contains_authoritative_request_id(
    node_debug_asgi_client: tuple[TestClient, MagicMock],
) -> None:
    """Node Debug 404 的响应体和响应头必须使用同一个 request_id。"""
    client, node_debug_service = node_debug_asgi_client
    node_debug_service.get_state = AsyncMock(side_effect=KeyError("missing"))

    session_id = "ses_00000000400070008000000000000002"
    response = client.get(
        "/api/v1/debug/node",
        params={"session_id": session_id},
        headers={
            "X-Local-Token": "local-dev-token",
            "X-Request-ID": "req_node_debug_missing",
        },
    )

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "req_node_debug_missing"
    assert response.json() == {
        "detail": "调试目标不存在: missing",
        "request_id": "req_node_debug_missing",
    }
    node_debug_service.get_state.assert_awaited_once_with(session_id, None)


def test_node_debug_validation_error_contains_authoritative_request_id(
    node_debug_asgi_client: tuple[TestClient, MagicMock],
) -> None:
    """请求参数校验错误也必须在响应体和响应头返回同一个 request_id。"""
    client, node_debug_service = node_debug_asgi_client
    node_debug_service.get_state = AsyncMock()
    response = client.get(
        "/api/v1/debug/node",
        params={"session_id": _SESSION_ID, "thread_id": "main"},
        headers={
            "X-Local-Token": "local-dev-token",
            "X-Request-ID": "req_node_debug_validation",
        },
    )

    assert response.status_code == 422
    assert response.headers["X-Request-ID"] == "req_node_debug_validation"
    body = response.json()
    assert body["request_id"] == "req_node_debug_validation"
    assert body["detail"][0]["loc"] == ["query", "thread_id"]
    node_debug_service.get_state.assert_not_awaited()


def test_node_debug_rejects_noncanonical_session_before_service(
    node_debug_asgi_client: tuple[TestClient, MagicMock],
) -> None:
    client, node_debug_service = node_debug_asgi_client
    node_debug_service.get_state = AsyncMock()

    response = client.get(
        "/api/v1/debug/node",
        params={"session_id": "ses_invalid"},
        headers={
            "X-Local-Token": "local-dev-token",
            "X-Request-ID": "req_node_debug_invalid_session",
        },
    )

    assert response.status_code == 422
    assert response.headers["X-Request-ID"] == "req_node_debug_invalid_session"
    assert response.json()["detail"][0]["loc"] == ["query", "session_id"]
    node_debug_service.get_state.assert_not_awaited()


@pytest.mark.parametrize(
    ("id_type", "valid_id"),
    [
        pytest.param(CanonicalSessionId, _SESSION_ID, id="session"),
        pytest.param(
            CanonicalThreadId,
            "thr_00000000700070008000000000000001",
            id="thread",
        ),
    ],
)
def test_canonical_id_schema_aliases_preserve_valid_values_and_reject_invalid(
    id_type: object,
    valid_id: str,
) -> None:
    adapter = TypeAdapter(id_type)

    assert adapter.validate_python(valid_id) == valid_id
    with pytest.raises(ValidationError):
        adapter.validate_python("invalid")


def test_http_exception_preserves_protocol_headers_and_structured_detail(
    node_debug_asgi_client: tuple[TestClient, MagicMock],
) -> None:
    """异常自带协议 header 保留，X-Request-ID 始终取 TraceMiddleware 的值。"""
    client, node_debug_service = node_debug_asgi_client
    thread_id = "thr_00000000700070008000000000000001"
    node_debug_service.get_state = AsyncMock(
        side_effect=HTTPException(
            status_code=401,
            detail={"reason": "需要认证", "scopes": ["debug"]},
            headers={
                "WWW-Authenticate": "Bearer",
                # 业务异常不能覆盖本次请求的权威追踪 ID；也覆盖大小写变体。
                "x-request-id": "spoofed-request-id",
            },
        )
    )
    response = client.get(
        "/api/v1/debug/node",
        params={
            "session_id": _SESSION_ID,
            "thread_id": thread_id,
        },
        headers={
            "X-Local-Token": "local-dev-token",
            "X-Request-ID": "req_node_debug_protocol_error",
        },
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["X-Request-ID"] == "req_node_debug_protocol_error"
    assert response.json() == {
        "detail": {"reason": "需要认证", "scopes": ["debug"]},
        "request_id": "req_node_debug_protocol_error",
    }
    node_debug_service.get_state.assert_awaited_once_with(_SESSION_ID, thread_id)


@pytest.mark.parametrize(
    ("error_type", "message", "expected_status"),
    [
        pytest.param(FileNotFoundError, "Session missing", 404, id="missing-session"),
        pytest.param(
            SessionDeletionPendingError,
            "session_deletion_pending: deleting",
            409,
            id="session-deleting",
        ),
    ],
)
def test_node_debug_state_maps_admission_errors(
    node_debug_asgi_client: tuple[TestClient, MagicMock],
    error_type: type[Exception],
    message: str,
    expected_status: int,
) -> None:
    """准入报告的缺失与删除中状态分别映射为 404 和 409。"""
    client, node_debug_service = node_debug_asgi_client
    node_debug_service.get_state = AsyncMock(side_effect=error_type(message))
    request_id = "req_node_debug_admission"

    response = client.get(
        "/api/v1/debug/node",
        params={"session_id": _SESSION_ID},
        headers={
            "X-Local-Token": "local-dev-token",
            "X-Request-ID": request_id,
        },
    )

    assert response.status_code == expected_status
    assert response.headers["X-Request-ID"] == request_id
    assert response.json()["request_id"] == request_id
    if expected_status == 404:
        assert response.json()["detail"] == message


def test_node_debug_state_keeps_unexpected_runtime_error_as_server_error(
    node_debug_asgi_client: tuple[TestClient, MagicMock],
) -> None:
    """准入以外的 RuntimeError 继续作为未处理故障暴露为 500。"""
    client, node_debug_service = node_debug_asgi_client
    node_debug_service.get_state = AsyncMock(
        side_effect=RuntimeError("unexpected state read failure")
    )

    response = client.get(
        "/api/v1/debug/node",
        params={"session_id": _SESSION_ID},
        headers={"X-Local-Token": "local-dev-token"},
    )

    assert response.status_code == 500
