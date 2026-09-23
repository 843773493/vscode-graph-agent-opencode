from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from app.gateway.auth import GatewayAuthContext
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime


@pytest.fixture(autouse=True)
def _hermetic_proxy_env(monkeypatch: pytest.MonkeyPatch):
    """R19 测试内 hermetic 修复：清掉本机代理环境变量。

    本文件用例不经外部网络；而环境的 NO_PROXY 含 IPv6 字面量（``::1``/
    ``[::1]``）会让用例内（或被测 gateway 代码内）构造的 httpx 客户端在
    代理解析时直接抛 ``InvalidURL: Invalid port: ':1]'``——属外部环境噪
    声混入测试。与 R18 对 gateway 会话上下文客户端的同类修复一致（任务
    书许可的 monkeypatch 清 ``*_proxy`` 做法）；清掉后客户端不取代理，
    与用例意图一致。
    """
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
from app.gateway.server.workspace_proxy import (
    _http_client,
    _is_streaming_workspace_path,
    _proxy_workspace_request,
    _stream_proxy_body,
    _stream_proxy_response,
    _wait_for_workspace_runtime,
)


class _StreamingResponse:
    def __init__(self) -> None:
        self.chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self.chunks.get()
            if chunk is None:
                return
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _FailingByteStream(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self._request = request

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        raise httpx.ReadError("upstream connection changed", request=self._request)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def registry(tmp_path: Path) -> GatewayWorkspaceRegistry:
    result = GatewayWorkspaceRegistry(storage_path=tmp_path / "workspaces.json")
    result.upsert(
        WorkspaceTarget(
            workspace_id="gw_stream",
            name="stream",
            root_path=str(tmp_path / "workspace"),
            backend_url="http://127.0.0.1:41001",
            connection_kind="local",
        )
    )
    return result


def test_backend_route_change_invalidates_existing_lease(
    registry: GatewayWorkspaceRegistry,
) -> None:
    lease = registry.route_lease("gw_stream")

    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_stream",
            name="stream",
            root_path="/tmp/workspace",
            backend_url="http://127.0.0.1:41002",
            connection_kind="local",
        ),
        activate=False,
    )

    assert lease.invalidated.is_set()
    assert registry.route_lease("gw_stream").revision > lease.revision


@pytest.mark.asyncio
async def test_sse_proxy_ends_immediately_when_route_is_invalidated(
    registry: GatewayWorkspaceRegistry,
) -> None:
    response = _StreamingResponse()
    lease = registry.route_lease("gw_stream")
    stream = _stream_proxy_response(
        cast(httpx.Response, response),
        lease,
        None,
    )

    await response.chunks.put(b": heartbeat\n\n")
    assert await asyncio.wait_for(anext(stream), timeout=0.2) == b": heartbeat\n\n"

    registry.invalidate_route("gw_stream")
    assert await asyncio.wait_for(anext(stream), timeout=0.2) == (
        b": gateway-route-invalidated\n\n"
    )
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=0.2)
    assert response.closed is True


@pytest.mark.asyncio
async def test_binary_proxy_forwards_chunks_and_closes_upstream() -> None:
    response = _StreamingResponse()
    stream = _stream_proxy_body(cast(httpx.Response, response))

    await response.chunks.put(b"binary-")
    await response.chunks.put(b"download")
    await response.chunks.put(None)
    assert b"".join([chunk async for chunk in stream]) == b"binary-download"
    assert response.closed is True


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("sessions/ses_1/traces/stream", True),
        ("session-catalog/events/stream", True),
        ("workspace/files/events", True),
        ("sessions/ses_1/turns/job_1/message-stream", True),
        ("sessions/ses_1/bootstrap", False),
        ("sessions/ses_1/history", False),
    ],
)
def test_streaming_workspace_paths_use_explicit_classification(
    path: str,
    expected: bool,
) -> None:
    assert _is_streaming_workspace_path(path) is expected


@pytest.mark.asyncio
async def test_streaming_proxy_uses_dedicated_http_client() -> None:
    application = FastAPI()
    normal_client = httpx.AsyncClient()
    streaming_client = httpx.AsyncClient()
    application.state.http_client = normal_client
    application.state.streaming_http_client = streaming_client
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/sessions/ses_1/traces/stream",
            "app": application,
            "headers": [],
        }
    )

    try:
        assert _http_client(request) is normal_client
        assert _http_client(request, streaming=True) is streaming_client
    finally:
        await normal_client.aclose()
        await streaming_client.aclose()


@pytest.mark.asyncio
async def test_message_stream_availability_retries_after_upstream_connection_change(
    registry: GatewayWorkspaceRegistry,
) -> None:
    application = FastAPI()
    calls: list[str] = []
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_stream",
            name="stream",
            root_path="/tmp/workspace",
            backend_url="http://127.0.0.1:41001",
            connection_kind="local",
        ),
        activate=False,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            registry.upsert(
                WorkspaceTarget(
                    workspace_id="gw_stream",
                    name="stream",
                    root_path="/tmp/workspace",
                    backend_url="http://127.0.0.1:41002",
                    connection_kind="local",
                ),
                activate=False,
            )
            return httpx.Response(
                status_code=200,
                headers={"content-type": "application/json"},
                stream=_FailingByteStream(request),
            )
        return httpx.Response(
            status_code=200,
            headers={"content-type": "application/json"},
            content=b'{"request_id":"req_availability","data":{"job_1":"strm_1"}}',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.registry = registry
    application.state.http_client = client
    application.state.streaming_http_client = client
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/sessions/ses_1/message-streams/availability",
            "query_string": b"turn_ids=job_1",
            "headers": [(b"x-boxteam-workspace-id", b"gw_stream")],
            "app": application,
        }
    )
    request.state.request_id = "req_availability"

    try:
        response = await _proxy_workspace_request(
            "sessions/ses_1/message-streams/availability",
            request,
            auth=None,
            user_access=None,
            include_credentials=False,
        )
    finally:
        await client.aclose()

    assert isinstance(response, Response)
    assert response.status_code == 200
    assert response.body == (
        b'{"request_id":"req_availability","data":{"job_1":"strm_1"}}'
    )
    assert calls == [
        "http://127.0.0.1:41001/api/v1/sessions/ses_1/message-streams/availability?turn_ids=job_1",
        "http://127.0.0.1:41002/api/v1/sessions/ses_1/message-streams/availability?turn_ids=job_1",
    ]


@pytest.mark.asyncio
async def test_message_stream_hides_initial_upstream_404_until_stream_is_created(
    registry: GatewayWorkspaceRegistry,
) -> None:
    application = FastAPI()
    calls: list[str] = []
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_stream",
            name="stream",
            root_path="/tmp/workspace",
            backend_url="http://127.0.0.1:41001",
            connection_kind="local",
        ),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41001"}
        ),
        activate=False,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(status_code=404, content=b"stream not ready")
        return httpx.Response(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            content=b": stream-ready\n\n",
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.registry = registry
    application.state.http_client = client
    application.state.streaming_http_client = client
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/sessions/ses_1/turns/job_1/message-stream",
            "query_string": b"after_seq=0",
            "headers": [(b"x-boxteam-workspace-id", b"gw_stream")],
            "app": application,
        }
    )
    request.state.request_id = "req_message_stream"

    try:
        response = await _proxy_workspace_request(
            "sessions/ses_1/turns/job_1/message-stream",
            request,
            auth=None,
            user_access=None,
            include_credentials=False,
        )
        body = b"".join([chunk async for chunk in response.body_iterator])
    finally:
        await client.aclose()

    assert isinstance(response, StreamingResponse)
    assert response.status_code == 200
    assert body == b": stream-ready\n\n"
    assert calls == [
        "http://127.0.0.1:41001/api/v1/sessions/ses_1/turns/job_1/message-stream?after_seq=0",
        "http://127.0.0.1:41001/api/v1/sessions/ses_1/turns/job_1/message-stream?after_seq=0",
    ]


@pytest.mark.asyncio
async def test_proxy_waits_for_active_workspace_restore_before_returning_503(
    registry: GatewayWorkspaceRegistry,
) -> None:
    application = FastAPI()
    workspace_root = Path("/tmp/workspace")
    runtime = WorkspaceRuntime(
        service_urls={"workspace_api": "http://127.0.0.1:41003"}
    )

    async def restore() -> None:
        await asyncio.sleep(0.01)
        registry.upsert(
            WorkspaceTarget(
                workspace_id="gw_stream",
                name="stream",
                root_path=str(workspace_root),
                backend_url="http://127.0.0.1:41003",
                connection_kind="local",
                managed=True,
                desired_running=True,
            ),
            runtime=runtime,
            activate=False,
        )

    application.state.managed_runtime_restore_tasks = {
        "gw_stream": asyncio.create_task(restore())
    }
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/workspace",
            "headers": [],
            "app": application,
        }
    )

    try:
        await _wait_for_workspace_runtime(request, registry.resolve("gw_stream"))
    finally:
        await asyncio.gather(
            *application.state.managed_runtime_restore_tasks.values(),
            return_exceptions=True,
        )

    assert registry.has_runtime("gw_stream") is True


@pytest.mark.asyncio
async def test_proxy_bounds_hung_upstream_response_headers_and_releases_connection(
    registry: GatewayWorkspaceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游接受连接却不回写响应头时，代理必须有界失败并取消上游请求。

    Gateway 的两个 httpx 客户端都不设读取超时（SSE 需要长期占用连接）。若不单独
    约束响应头等待，上游挂起会让浏览器请求永久挂起，代理任务与上游连接一起泄漏。
    """

    application = FastAPI()
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_stream",
            name="stream",
            root_path="/tmp/workspace",
            backend_url="http://127.0.0.1:41001",
            connection_kind="local",
        ),
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41001"}
        ),
        activate=False,
    )

    released = asyncio.Event()
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            # 模拟“已接受 TCP 但永不回写响应头”的上游。
            await asyncio.Event().wait()
        finally:
            released.set()
        raise AssertionError("上游挂起分支不应返回响应")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.registry = registry
    application.state.http_client = client
    application.state.streaming_http_client = client
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/workspace",
            "query_string": b"",
            "headers": [(b"x-boxteam-workspace-id", b"gw_stream")],
            "app": application,
        }
    )
    request.state.request_id = "req_hung_upstream"
    monkeypatch.setattr(
        "app.gateway.proxy_upstream.UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS",
        0.2,
    )

    try:
        with pytest.raises(HTTPException) as captured:
            # 用测试侧上限把回归固定成确定性失败：若代理失去响应头超时，
            # 这里会抛 TimeoutError 而不是让整个用例挂死。
            await asyncio.wait_for(
                _proxy_workspace_request(
                    "workspace",
                    request,
                    auth=None,
                    user_access=None,
                    include_credentials=False,
                ),
                timeout=2,
            )
    finally:
        await client.aclose()

    assert captured.value.status_code == 504
    assert "有限等待时间内未返回响应头" in str(captured.value.detail)
    assert entered.is_set() is True
    # 取消必须传导到上游请求，否则挂起的上游连接会随代理任务一起泄漏。
    await asyncio.wait_for(released.wait(), timeout=1)


@pytest.mark.asyncio
async def test_auxiliary_proxy_also_bounds_hung_upstream_response_headers(
    registry: GatewayWorkspaceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """辅助服务代理与工作区 API 代理共用同一响应头上限，不能各自漏掉。"""

    from app.gateway.auxiliary_proxy import proxy_auxiliary_http

    application = FastAPI()
    registry.upsert(
        WorkspaceTarget(
            workspace_id="gw_stream",
            name="stream",
            root_path="/tmp/workspace",
            backend_url="http://127.0.0.1:41001",
            connection_kind="local",
        ),
        runtime=WorkspaceRuntime(
            service_urls={
                "workspace_api": "http://127.0.0.1:41001",
                "terminal_manager": "http://127.0.0.1:41002",
            }
        ),
        activate=False,
    )

    released = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
        finally:
            released.set()
        raise AssertionError("上游挂起分支不应返回响应")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.registry = registry
    application.state.http_client = client
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/gateway/workspaces/gw_stream/terminal-manager/api/terminals",
            "query_string": b"",
            "headers": [],
            "app": application,
        }
    )
    # 辅助代理会读取请求体；这里显式给出空体，避免依赖 ASGI receive 通道。
    request._body = b""
    request.state.request_id = "req_hung_auxiliary"
    monkeypatch.setattr(
        "app.gateway.proxy_upstream.UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS",
        0.2,
    )

    try:
        with pytest.raises(HTTPException) as captured:
            await asyncio.wait_for(
                proxy_auxiliary_http(
                    workspace_id="gw_stream",
                    service_path="terminal-manager",
                    path="api/terminals",
                    request=request,
                    auth=GatewayAuthContext(kind="local"),
                ),
                timeout=2,
            )
    finally:
        await client.aclose()

    assert captured.value.status_code == 504
    assert "辅助服务在有限等待时间内未返回响应头" in str(captured.value.detail)
    await asyncio.wait_for(released.wait(), timeout=1)


@pytest.mark.asyncio
async def test_proxy_rejects_path_that_folds_out_of_v1_namespace(
    registry: GatewayWorkspaceRegistry,
) -> None:
    """%2e%2e 折叠逃出 /api/v1 命名空间必须响亮失败，不得静默转发到工作区后端。

    uvicorn 会把请求行里的 %2e%2e 解码成 ..，httpx 再按 RFC 3986 折叠点段，两者
    叠加会让 /api/v1/%2e%2e/%2e%2e/api/gateway/health 折叠成 /api/gateway/health。
    若代理不校验前缀，浏览器就能让 Gateway 把请求（含远程目标的联邦凭据）打到
    工作区后端的控制面命名空间。
    """

    application = FastAPI()
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"{}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.registry = registry
    application.state.http_client = client
    application.state.streaming_http_client = client
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/%2e%2e/%2e%2e/api/gateway/health",
            "headers": [(b"x-boxteam-workspace-id", b"gw_stream")],
            "app": application,
        }
    )
    request.state.request_id = "req_traversal"

    try:
        with pytest.raises(HTTPException) as captured:
            await _proxy_workspace_request(
                "../../api/gateway/health",
                request,
                auth=None,
                user_access=None,
                include_credentials=False,
            )
    finally:
        await client.aclose()

    assert captured.value.status_code == 400
    assert "越出上游命名空间" in str(captured.value.detail)
    # 关键：没有任何请求被转发到上游。
    assert calls == []
    assert registry.route_reference_counts("gw_stream") == (0, 0)


@pytest.mark.asyncio
async def test_stream_proxy_releases_route_reference_under_anyio_cancellation(
    registry: GatewayWorkspaceRegistry,
) -> None:
    """客户端断开触发 anyio task group 取消时，上游响应与路由引用仍必须被释放。

    uvicorn 声明 ASGI spec 2.3，starlette 的 StreamingResponse 用 anyio task group
    驱动响应体；客户端断开时 cancel scope 取消流任务，finally 里每个 await 都会
    立刻重新抛 CancelledError。清理若不做 shield，aclose 与 on_close 会被跳过，
    路由引用计数不归零，工作区重启/删除随后被「仍有代理引用」永久挡住。
    """

    import anyio

    closed = asyncio.Event()

    class _Upstream:
        def aiter_bytes(self) -> AsyncIterator[bytes]:
            async def iterate():
                while True:
                    await asyncio.sleep(0.01)
                    yield b": tick\n\n"

            return iterate()

        async def aclose(self) -> None:
            closed.set()

    lease = registry.acquire_route_reference("gw_stream", streaming=True)
    released: list[str] = []

    def on_close() -> None:
        released.append("on_close")
        registry.release_route_reference("gw_stream", streaming=True)

    stream = _stream_proxy_response(
        cast(httpx.Response, _Upstream()),
        lease,
        None,
        on_close,
    )
    disconnected = asyncio.Event()

    async def stream_response() -> None:
        async for _ in stream:
            await asyncio.sleep(0)

    async def wait_for_disconnect() -> None:
        await disconnected.wait()

    async with anyio.create_task_group() as task_group:

        async def wrap(func) -> None:
            await func()
            task_group.cancel_scope.cancel()

        task_group.start_soon(wrap, stream_response)
        await asyncio.sleep(0.1)
        disconnected.set()
        await wrap(wait_for_disconnect)

    assert await asyncio.wait_for(closed.wait(), timeout=1)
    assert released == ["on_close"]
    assert registry.route_reference_counts("gw_stream") == (0, 0)
