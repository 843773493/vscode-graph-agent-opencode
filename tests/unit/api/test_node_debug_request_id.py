from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_node_debug_service
from app.main import app
from app.services.infrastructure.node_debug_service import NodeDebugService


def test_node_debug_http_error_contains_authoritative_request_id() -> None:
    """Node Debug 404 的响应体和响应头必须使用同一个 request_id。"""
    node_debug_service = MagicMock(spec=NodeDebugService)
    node_debug_service.get_state = AsyncMock(side_effect=KeyError("missing"))
    app.dependency_overrides[get_node_debug_service] = lambda: node_debug_service

    try:
        response = TestClient(app).get(
            "/api/v1/debug/node",
            params={"session_id": "missing", "thread_id": "main"},
            headers={
                "X-Local-Token": "local-dev-token",
                "X-Request-ID": "req_node_debug_missing",
            },
        )
    finally:
        app.dependency_overrides.pop(get_node_debug_service, None)

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "req_node_debug_missing"
    assert response.json() == {
        "detail": "调试目标不存在: missing",
        "request_id": "req_node_debug_missing",
    }


def test_node_debug_validation_error_contains_authoritative_request_id() -> None:
    """请求参数校验错误也必须在响应体和响应头返回同一个 request_id。"""
    node_debug_service = MagicMock(spec=NodeDebugService)
    app.dependency_overrides[get_node_debug_service] = lambda: node_debug_service

    try:
        response = TestClient(app).get(
            "/api/v1/debug/node",
            params={"session_id": "ses_test"},
            headers={
                "X-Local-Token": "local-dev-token",
                "X-Request-ID": "req_node_debug_validation",
            },
        )
    finally:
        app.dependency_overrides.pop(get_node_debug_service, None)

    assert response.status_code == 422
    assert response.headers["X-Request-ID"] == "req_node_debug_validation"
    body = response.json()
    assert body["request_id"] == "req_node_debug_validation"
    assert body["detail"][0]["loc"] == ["query", "thread_id"]


def test_http_exception_preserves_protocol_headers_and_structured_detail() -> None:
    """异常自带协议 header 保留，X-Request-ID 始终取 TraceMiddleware 的值。"""
    node_debug_service = MagicMock(spec=NodeDebugService)
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
    app.dependency_overrides[get_node_debug_service] = lambda: node_debug_service

    try:
        response = TestClient(app).get(
            "/api/v1/debug/node",
            params={"session_id": "ses_test", "thread_id": "main"},
            headers={
                "X-Local-Token": "local-dev-token",
                "X-Request-ID": "req_node_debug_protocol_error",
            },
        )
    finally:
        app.dependency_overrides.pop(get_node_debug_service, None)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers["X-Request-ID"] == "req_node_debug_protocol_error"
    assert response.json() == {
        "detail": {"reason": "需要认证", "scopes": ["debug"]},
        "request_id": "req_node_debug_protocol_error",
    }
