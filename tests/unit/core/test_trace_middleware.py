from __future__ import annotations

import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_request_id
from app.core.trace_middleware import (
    GATEWAY_ID_HEADER,
    TraceMiddleware,
    get_current_gateway_id,
    require_current_gateway_id,
)
from app.schemas.internal_v2.common import APIResponse


def _build_client(*, raise_server_exceptions: bool = True) -> TestClient:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)

    @app.get("/request-id")
    async def request_id_endpoint(
        request_id: str = Depends(get_request_id),
    ) -> APIResponse[dict[str, str]]:
        return APIResponse(data={"request_id": request_id}, request_id=request_id)

    @app.get("/failure")
    async def failure_endpoint() -> None:
        raise RuntimeError("测试请求失败")

    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_generated_request_id_is_identical_in_header_and_body() -> None:
    response = _build_client().get("/request-id")

    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    assert request_id
    assert response.json()["request_id"] == request_id
    assert response.json()["data"]["request_id"] == request_id


def test_incoming_request_id_is_used_as_the_single_authority() -> None:
    response = _build_client().get(
        "/request-id",
        headers={"X-Request-ID": "req_from_client"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_from_client"
    assert response.json()["request_id"] == "req_from_client"


def test_successful_request_trace_is_emitted_at_debug_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="app.core.trace_middleware")

    response = _build_client().get(
        "/request-id",
        headers={"X-Request-ID": "req_trace_log"},
    )

    assert response.status_code == 200
    assert "[TRACE] method=GET path=/request-id status=200" in caplog.text
    assert "request_id=req_trace_log" in caplog.text


def test_failed_request_trace_is_emitted_with_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="app.core.trace_middleware")

    with _build_client(raise_server_exceptions=False) as client:
        response = client.get(
            "/failure",
            headers={"X-Request-ID": "req_trace_failure"},
        )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "req_trace_failure"
    assert response.json() == {
        "code": 500,
        "message": "RuntimeError: 测试请求失败",
        "data": None,
        "request_id": "req_trace_failure",
    }
    assert "[TRACE] method=GET path=/failure status=500" in caplog.text
    assert "request_id=req_trace_failure" in caplog.text
    assert "测试请求失败" in caplog.text


def _build_gateway_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)

    @app.get("/gateway-id")
    async def gateway_id_endpoint() -> APIResponse[dict[str, str | None]]:
        return APIResponse(
            data={"gateway_id": get_current_gateway_id()}, request_id="req"
        )

    @app.get("/require-gateway-id")
    async def require_gateway_id_endpoint() -> dict[str, str]:
        return {"gateway_id": require_current_gateway_id("测试消费方")}

    return TestClient(app, raise_server_exceptions=False)


def test_gateway_id_header_is_read_into_request_scope() -> None:
    """请求级注入通道：带头即绑定，缺头即保持未绑定。"""
    client = _build_gateway_client()

    with_header = client.get(
        "/gateway-id", headers={GATEWAY_ID_HEADER: "gateway_abc"}
    )
    assert with_header.status_code == 200
    assert with_header.json()["data"]["gateway_id"] == "gateway_abc"
    assert with_header.headers["X-Request-ID"]

    without_header = client.get("/gateway-id")
    assert without_header.status_code == 200
    assert without_header.json()["data"]["gateway_id"] is None

    blank_header = client.get("/gateway-id", headers={GATEWAY_ID_HEADER: "   "})
    assert blank_header.json()["data"]["gateway_id"] is None


def test_require_gateway_id_fails_closed_without_header() -> None:
    """缺头时消费方 fail-closed，MUST NOT 回退任何虚假默认值。"""
    client = _build_gateway_client()

    assert client.get("/require-gateway-id").status_code == 500

    ok = client.get(
        "/require-gateway-id", headers={GATEWAY_ID_HEADER: "gateway_abc"}
    )
    assert ok.status_code == 200
    assert ok.json() == {"gateway_id": "gateway_abc"}


def test_gateway_id_does_not_leak_across_requests() -> None:
    """按请求注入而非进程级单例：后一个请求不得继承前一个请求的绑定。"""
    client = _build_gateway_client()

    first = client.get(
        "/gateway-id", headers={GATEWAY_ID_HEADER: "gateway_one"}
    )
    assert first.json()["data"]["gateway_id"] == "gateway_one"

    second = client.get("/gateway-id")
    assert second.json()["data"]["gateway_id"] is None
