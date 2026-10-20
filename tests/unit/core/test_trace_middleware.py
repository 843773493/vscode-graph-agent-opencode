from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from app.api.deps import get_request_id
from app.core.trace_middleware import (
    GATEWAY_ID_HEADER,
    TraceMiddleware,
    get_current_gateway_id,
    get_current_navigation_actor,
    get_current_request_id,
    require_current_gateway_id,
    require_current_request_id,
    reset_current_request_id,
    set_current_request_id,
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

    @app.get("/http-exception")
    async def http_exception_endpoint() -> None:
        raise HTTPException(status_code=409, detail="业务冲突")

    @app.get("/current-request-id")
    async def current_request_id_endpoint() -> APIResponse[dict[str, str]]:
        # 模拟 start_job 在请求作用域内读取权威 request_id 并随 job 携带。
        observed = get_current_request_id()
        return APIResponse(data={"observed": observed or ""}, request_id=observed or "")

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


def test_http_exception_keeps_its_status_and_request_id() -> None:
    response = _build_client().get(
        "/http-exception",
        headers={"X-Request-ID": "req_http_exception"},
    )

    assert response.status_code == 409
    assert response.headers["X-Request-ID"] == "req_http_exception"
    assert response.json() == {"detail": "业务冲突"}


def test_request_id_is_bound_to_request_scope_contextvar() -> None:
    """job 创建点从请求级 ContextVar 读取权威 request_id，不补造第二个。"""
    response = _build_client().get(
        "/current-request-id",
        headers={"X-Request-ID": "req_carried_into_job"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["observed"] == "req_carried_into_job"


def test_request_scope_contextvar_is_reset_after_dispatch() -> None:
    """请求结束后执行根不再残留 request_id，独立执行根必须显式携带。"""
    _build_client().get("/current-request-id", headers={"X-Request-ID": "req_reset"})

    assert get_current_request_id() is None


def test_require_current_request_id_fails_closed_without_binding() -> None:
    """无请求绑定即 fail-closed 抛错，且消息带消费者名；MUST NOT 回退虚假默认值。"""
    assert get_current_request_id() is None

    with pytest.raises(RuntimeError) as excinfo:
        require_current_request_id("测试消费方")

    message = str(excinfo.value)
    assert "测试消费方" in message
    assert "未绑定" in message


def test_require_current_request_id_returns_bound_authority_verbatim() -> None:
    """与宽容版读同一 ContextVar：绑定后原样返回；复位后回到 fail-closed。"""
    token = set_current_request_id("req_bound_authority")
    try:
        assert get_current_request_id() == "req_bound_authority"
        assert require_current_request_id("测试消费方") == "req_bound_authority"
    finally:
        reset_current_request_id(token)

    assert get_current_request_id() is None
    with pytest.raises(RuntimeError):
        require_current_request_id("测试消费方")


def test_require_current_request_id_rejects_blank_binding() -> None:
    """空串等同无绑定：fail-closed 不把空串当合法权威 ID（与 gateway_id 守卫一致）。"""
    token = set_current_request_id("")
    try:
        with pytest.raises(RuntimeError):
            require_current_request_id("测试消费方")
    finally:
        reset_current_request_id(token)


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
        "/gateway-id", headers={GATEWAY_ID_HEADER: "gateway_0123456789abcdef0123456789abcdef"}
    )
    assert with_header.status_code == 200
    assert with_header.json()["data"]["gateway_id"] == "gateway_0123456789abcdef0123456789abcdef"
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
        "/require-gateway-id", headers={GATEWAY_ID_HEADER: "gateway_0123456789abcdef0123456789abcdef"}
    )
    assert ok.status_code == 200
    assert ok.json() == {"gateway_id": "gateway_0123456789abcdef0123456789abcdef"}


def test_gateway_id_does_not_leak_across_requests() -> None:
    """按请求注入而非进程级单例：后一个请求不得继承前一个请求的绑定。"""
    client = _build_gateway_client()

    first = client.get(
        "/gateway-id", headers={GATEWAY_ID_HEADER: "gateway_11111111111111111111111111111111"}
    )
    assert first.json()["data"]["gateway_id"] == "gateway_11111111111111111111111111111111"

    second = client.get("/gateway-id")
    assert second.json()["data"]["gateway_id"] is None


@pytest.mark.parametrize("gateway_id", [
    "local", "8014", "127.0.0.1:8014", "localhost", "gateway_abc",
    "gateway_" + "a" * 31, "gateway_" + "g" * 32, "gateway_" + "A" * 32,
    "channel_epoch_1",
])
def test_gateway_identity_consumers_reject_invalid_header(gateway_id: str) -> None:
    client = _build_gateway_client()
    for path in ("/gateway-id", "/require-gateway-id"):
        response = client.get(path, headers={GATEWAY_ID_HEADER: gateway_id, "X-Request-ID": "invalid-gateway"})
        assert response.status_code == 500
        assert "非法 gateway_id" in response.json()["message"]
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == "invalid-gateway"
    assert get_current_gateway_id() is None


@pytest.mark.asyncio
async def test_cancelled_request_resets_trace_contextvars() -> None:
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/cancel",
        "raw_path": b"/cancel",
        "query_string": b"",
        "headers": [
            (b"x-request-id", b"req_cancel"),
            (GATEWAY_ID_HEADER.lower().encode(), b"gateway_cccccccccccccccccccccccccccccccc"),
            (b"x-boxteam-navigation-actor", b"actor_cancel"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 80),
    }

    async def app(_: Scope, __: Receive, ___: Send) -> None:
        assert get_current_request_id() == "req_cancel"
        assert get_current_gateway_id() == "gateway_cccccccccccccccccccccccccccccccc"
        assert get_current_navigation_actor() == "actor_cancel"
        raise asyncio.CancelledError("请求取消")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_: Message) -> None:
        raise AssertionError("取消的请求不应发送响应")

    with pytest.raises(asyncio.CancelledError, match="请求取消"):
        await TraceMiddleware(app)(scope, receive, send)

    assert get_current_request_id() is None
    assert get_current_gateway_id() is None
    assert get_current_navigation_actor() is None


@pytest.mark.asyncio
async def test_stream_chunk_is_forwarded_and_started_response_error_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="app.core.trace_middleware")
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/events",
        "raw_path": b"/events",
        "query_string": b"",
        "headers": [(b"x-request-id", b"req_stream")],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 80),
    }
    first_chunk_sent = asyncio.Event()
    release_app = asyncio.Event()
    messages: list[Message] = []

    async def app(_: Scope, __: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b"data: first\n\n", "more_body": True}
        )
        first_chunk_sent.set()
        await release_app.wait()
        raise RuntimeError("流式响应开始后失败")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    task = asyncio.create_task(TraceMiddleware(app)(scope, receive, send))
    try:
        await asyncio.wait_for(first_chunk_sent.wait(), timeout=1)
        assert [message["type"] for message in messages] == [
            "http.response.start",
            "http.response.body",
        ]
        assert dict(messages[0]["headers"])[b"x-request-id"] == b"req_stream"
        assert messages[1]["body"] == b"data: first\n\n"
        release_app.set()
        with pytest.raises(RuntimeError, match="流式响应开始后失败"):
            await task
        record = next(
            record
            for record in caplog.records
            if "request_id=req_stream" in record.getMessage()
        )
        assert "response_started=true" in record.getMessage()
        assert "status=200" in record.getMessage()
        assert record.exc_info is not None
        assert record.exc_info[1] is not None
        assert str(record.exc_info[1]) == "流式响应开始后失败"
    finally:
        release_app.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_non_http_scope_is_passed_through() -> None:
    scope: Scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
    downstream_scopes: list[Scope] = []
    messages: list[Message] = []

    async def app(scope: Scope, _: Receive, send: Send) -> None:
        downstream_scopes.append(scope)
        await send({"type": "lifespan.startup.complete"})

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        messages.append(message)

    await TraceMiddleware(app)(scope, receive, send)

    assert len(downstream_scopes) == 1
    assert downstream_scopes[0] is scope
    assert messages == [{"type": "lifespan.startup.complete"}]
