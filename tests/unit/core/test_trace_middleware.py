from __future__ import annotations

import logging

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_request_id
from app.core.trace_middleware import TraceMiddleware
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
