from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import anyio
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from app.gateway.auth import GatewayAuthContext
from app.gateway.auxiliary_proxy import _stream_response, proxy_auxiliary_http
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime


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
        ),
        runtime=WorkspaceRuntime(
            service_urls={
                "workspace_api": "http://127.0.0.1:41100",
                "terminal_manager": "http://127.0.0.1:41101/api",
            }
        ),
        activate=False,
    )
    return result


@pytest.mark.asyncio
async def test_auxiliary_proxy_rejects_path_folded_out_of_service_prefix(
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

    assert captured.value.status_code == 400
    detail = str(captured.value.detail)
    assert "越出上游命名空间" in detail or "点段" in detail
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
