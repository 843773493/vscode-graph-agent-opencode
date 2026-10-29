from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import anyio
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.types import Message
from starlette.websockets import WebSocket

from app.gateway.auth import GatewayAuthContext
from app.gateway.auxiliary_proxy import (
    _proxy_auxiliary_websocket,
    _stream_response,
    proxy_attach_frontend,
    proxy_auxiliary_http,
)
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.service_types import GatewayServiceName


@pytest.fixture
def registry(tmp_path) -> GatewayWorkspaceRegistry:
    result = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    result.upsert(
        WorkspaceTarget(
            workspace_id="gw_aux",
            name="aux",
            root_path=str(tmp_path / "workspace"),
            backend_url="http://127.0.0.1:41100",
            connection_kind="local",
            backend_workspace_id="00000000-0000-4000-8000-000000000001",
        ),
        runtime=WorkspaceRuntime(
            service_urls={
                "workspace_api": "http://127.0.0.1:41100",
                "terminal_manager": "http://127.0.0.1:41101/api",
                "browser_manager": "http://127.0.0.1:41102/api",
            }
        ),
        activate=False,
    )
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_kind", ["auxiliary", "attach"])
async def test_http_proxies_preserve_repeated_query_parameters(
    registry: GatewayWorkspaceRegistry,
    proxy_kind: str,
) -> None:
    """HTTPX 转发必须保留同名参数的所有值、空值与解码后的文本。"""

    application = FastAPI()
    queries: list[list[tuple[str, str]]] = []
    request_ids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params.multi_items())
        request_ids.append(request.headers["X-Request-ID"])
        return httpx.Response(200, content=b"upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        application.state.registry = registry
        application.state.http_client = client
        application.state.attach_frontend_urls = {"terminal": "http://127.0.0.1:41102"}
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/health",
                "query_string": b"item=first&item=second&empty=&label=%E4%B8%AD%E6%96%87",
                "headers": [],
                "app": application,
            }
        )
        request._body = b""
        request.state.request_id = "req_aux_query"
        if proxy_kind == "auxiliary":
            response = await proxy_auxiliary_http(
                workspace_id="gw_aux",
                service_path="terminal-manager",
                path="health",
                request=request,
                auth=GatewayAuthContext(kind="local"),
            )
        else:
            response = await proxy_attach_frontend(
                kind="terminal", path="index.html", request=request
            )

    assert response.status_code == 200
    assert response.body == b"upstream"
    assert queries == [[("item", "first"), ("item", "second"), ("empty", ""), ("label", "中文")]]
    assert request_ids == ["req_aux_query"]
    assert registry.route_reference_counts("gw_aux") == (0, 0)


@pytest.mark.asyncio
async def test_auxiliary_proxy_rejects_direct_resource_management_before_forwarding(
    registry: GatewayWorkspaceRegistry,
) -> None:
    """远端辅助服务带路径前缀时，%2e%2e 折叠越出前缀必须响亮失败而不是转发。"""

    application = FastAPI()
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.registry = registry
    application.state.http_client = client
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/gateway/workspaces/gw_aux/terminal-manager/evil",
            "query_string": b"",
            "headers": [],
            "app": application,
        }
    )
    request._body = b""
    request.state.request_id = "req_aux_traversal"

    try:
        with pytest.raises(HTTPException) as captured:
            await proxy_auxiliary_http(
                workspace_id="gw_aux",
                service_path="terminal-manager",
                # uvicorn 已把请求行里的 %2e%2e 解码为 ..，路由拿到的就是解码后的路径。
                path="../../evil",
                request=request,
                auth=GatewayAuthContext(kind="local"),
            )
    finally:
        await client.aclose()

    assert captured.value.status_code == 403
    detail = str(captured.value.detail)
    assert "/api/v1/resources" in detail
    assert calls == []
    assert registry.route_reference_counts("gw_aux") == (0, 0)


@pytest.mark.asyncio
async def test_auxiliary_stream_releases_upstream_under_anyio_cancellation() -> None:
    """辅助服务 SSE 在客户端断开（anyio task group 取消）时仍必须关闭上游。"""

    closed = asyncio.Event()

    class _Upstream:
        def aiter_bytes(self) -> AsyncIterator[bytes]:
            async def iterate():
                yield b": first"
                await asyncio.sleep(5)
                yield b": never"

            return iterate()

        async def aclose(self) -> None:
            # 真实 aclose 会 await I/O；用 await 让取消点落在这里。
            await asyncio.sleep(0.02)
            closed.set()

    released: list[str] = []

    def on_close() -> None:
        released.append("on_close")

    stream = _stream_response(cast(httpx.Response, _Upstream()), on_close)
    disconnected = asyncio.Event()

    async def consume() -> None:
        async for _ in stream:
            await asyncio.sleep(0)

    async def wait_for_disconnect() -> None:
        await disconnected.wait()

    async with anyio.create_task_group() as task_group:

        async def wrap(func) -> None:
            await func()
            task_group.cancel_scope.cancel()

        task_group.start_soon(wrap, consume)
        await asyncio.sleep(0.1)
        disconnected.set()
        await wrap(wait_for_disconnect)

    assert await asyncio.wait_for(closed.wait(), timeout=1)
    assert released == ["on_close"]


@pytest.mark.asyncio
async def test_auxiliary_http_releases_reference_when_registry_raises_runtime(
    registry: GatewayWorkspaceRegistry,
) -> None:
    """registry 权威拒绝（RuntimeError）必须归还引用并显式失败，不得泄漏。"""

    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_bad",
            name="bad",
            root_path=None,
            backend_url="",
            connection_kind="remote_gateway",
            backend_workspace_id="00000000-0000-4000-8000-000000000002",
            owner="manual",
            remote_gateway_connection_id=None,
            remote_workspace_id=None,
        ),
        activate=False,
    )
    application = FastAPI()
    application.state.registry = registry
    application.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"ok")
        )
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/gateway/workspaces/gw_bad/terminal-manager/health",
            "query_string": b"",
            "headers": [],
            "app": application,
        }
    )
    request._body = b""
    request.state.request_id = "req_aux_runtime_error"

    with pytest.raises(HTTPException) as captured:
        await proxy_auxiliary_http(
            workspace_id="gw_bad",
            service_path="terminal-manager",
            path="health",
            request=request,
            auth=GatewayAuthContext(kind="local"),
        )

    assert captured.value.status_code == 503
    assert registry.route_reference_counts("gw_bad") == (0, 0)


@pytest.fixture
def auxiliary_websocket(registry: GatewayWorkspaceRegistry) -> tuple[WebSocket, list[Message]]:
    """使用真实 ASGI WebSocket 状态机，记录关闭帧而不启动网络服务。"""
    from app.gateway.auth import get_gateway_local_token

    application = FastAPI()
    application.state.registry = registry
    messages: list[Message] = []
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})

    async def receive() -> Message:
        return await incoming.get()

    async def send(message: Message) -> None:
        messages.append(message)

    return WebSocket({
        "type": "websocket", "path": "/terminal", "headers": [], "app": application,
        "query_string": f"token={get_gateway_local_token()}".encode(),
    }, receive, send), messages


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["terminal_manager", "browser_manager"])
@pytest.mark.parametrize("native", ["/private/用户/socket.pem", r"C:\私有\socket.pem", r"\\server\私有\socket.pem"])
@pytest.mark.parametrize("phase,code", [("access", 1008), ("runtime", 1011), ("route", 1008), ("upstream", 1011), ("relay", 1011)])
async def test_auxiliary_websocket_failures_keep_native_diagnostics_private(
    registry: GatewayWorkspaceRegistry,
    auxiliary_websocket: tuple[WebSocket, list[Message]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    service: GatewayServiceName,
    native: str,
    phase: str,
    code: int,
) -> None:
    """原始错误不得进入关闭帧；诊断保留全文和实例，引用仍恰好归还。"""
    websocket, messages = auxiliary_websocket
    resolve = registry.resolve
    original: Exception
    if phase in {"access", "route"}:
        original = ValueError(f"读取失败 {native} " + "完整错误" * 100)
    elif phase == "runtime":
        original = RuntimeError(f"读取失败 {native} " + "完整错误" * 100)
    else:
        original = PermissionError(13, f"读取失败 {native} " + "完整错误" * 100, native)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise original

    peer_closed: list[bool] = []
    receiver_cancelled: list[bool] = []
    if phase in {"access", "runtime"}:
        monkeypatch.setattr(registry, "resolve", fail)
    elif phase == "route":
        monkeypatch.setattr(registry, "resolve_service_url", fail)
    else:
        class Upstream:
            async def send(self, _message: str | bytes) -> None:
                raise AssertionError("本场景不发送客户端数据")

            def __aiter__(self) -> AsyncIterator[str | bytes]:
                async def iterate() -> AsyncIterator[str | bytes]:
                    try:
                        await asyncio.sleep(0)
                        raise original
                        yield b""  # pragma: no cover - 声明异步迭代协议。
                    finally:
                        receiver_cancelled.append(True)
                return iterate()

        class Connect:
            async def __aenter__(self) -> Upstream:
                assert registry.route_reference_counts("gw_aux") == (0, 1)
                if phase == "upstream":
                    raise original
                return Upstream()

            async def __aexit__(self, *_args: object) -> None:
                peer_closed.append(True)

        monkeypatch.setattr("app.gateway.auxiliary_proxy.connect", lambda *_args, **_kwargs: Connect())

    await _proxy_auxiliary_websocket(
        websocket=websocket, workspace_id="gw_aux", service=service, socket_path="terminal",
    )
    if phase in {"access", "runtime"}:
        monkeypatch.setattr(registry, "resolve", resolve)

    frame = messages[-1]
    assert frame["type"] == "websocket.close" and frame["code"] == code
    assert native not in frame["reason"]
    reported_phase = {"runtime": "access", "relay": "upstream"}.get(phase, phase)
    assert frame["reason"] == f"auxiliary_websocket_{reported_phase}_failed"
    assert len(frame["reason"].encode()) <= 123
    assert native not in str(messages)
    assert registry.route_reference_counts("gw_aux") == (0, 0)
    assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
    assert str(original) in caplog.text
    if phase == "relay":
        assert peer_closed == [True] and receiver_cancelled == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["token", "workspace"])
async def test_auxiliary_websocket_rejection_never_opens_upstream(
    registry: GatewayWorkspaceRegistry,
    auxiliary_websocket: tuple[WebSocket, list[Message]],
    monkeypatch: pytest.MonkeyPatch,
    rejection: str,
) -> None:
    websocket, messages = auxiliary_websocket
    if rejection == "token":
        websocket.scope["query_string"] = b"token=wrong"
    else:
        websocket.scope["headers"] = [(b"x-boxteam-backend-workspace-id", b"wrong")]

    def unexpected_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("拒绝访问不能连接上游")

    monkeypatch.setattr("app.gateway.auxiliary_proxy.connect", unexpected_connect)
    await _proxy_auxiliary_websocket(
        websocket=websocket, workspace_id="gw_aux", service="terminal_manager", socket_path="terminal",
    )
    assert messages == [{"type": "websocket.close", "code": 1008, "reason": "auxiliary_websocket_access_failed"}]
    assert registry.route_reference_counts("gw_aux") == (0, 0)


@pytest.mark.asyncio
async def test_auxiliary_websocket_cancellation_preserves_identity_and_reference(
    registry: GatewayWorkspaceRegistry,
    auxiliary_websocket: tuple[WebSocket, list[Message]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket, messages = auxiliary_websocket
    original = asyncio.CancelledError("原请求取消")

    class Connect:
        async def __aenter__(self) -> None:
            assert registry.route_reference_counts("gw_aux") == (0, 1)
            raise original

        async def __aexit__(self, *_args: object) -> None:
            raise AssertionError("未建立的连接不执行退出")

    monkeypatch.setattr("app.gateway.auxiliary_proxy.connect", lambda *_args, **_kwargs: Connect())
    with pytest.raises(asyncio.CancelledError) as captured:
        await _proxy_auxiliary_websocket(
            websocket=websocket, workspace_id="gw_aux", service="terminal_manager", socket_path="terminal",
        )
    assert captured.value is original
    assert messages == [{"type": "websocket.accept", "subprotocol": None, "headers": []}]
    assert registry.route_reference_counts("gw_aux") == (0, 0)
