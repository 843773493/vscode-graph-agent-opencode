"""跨两跳真实 ASGI 传输核对错误来源；不启动服务、不访问用户工作区。"""

import json
import sqlite3
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest
from boxteam_plugin_sdk.http import get_request_id
from fastapi import FastAPI, HTTPException, Request, Response

from app.core.failures import HTTP_FAILURE_HEADER
from app.core.trace_middleware import TraceMiddleware
from app.gateway.main import gateway_http_exception_handler
from app.gateway.proxy_upstream import filter_hop_by_hop_headers, send_upstream_request

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
GATEWAY_A = "gateway_" + "a" * 32
GATEWAY_B = "gateway_" + "b" * 32


@pytest.fixture
def backend() -> FastAPI:
    app = FastAPI()
    app.state.backend_id = "backend-B2"
    app.state.workspace_container_registry = SimpleNamespace(workspace_ids=(WORKSPACE_ID,))
    app.add_middleware(TraceMiddleware, component="workspace_backend")

    @app.get("/catalog")
    async def catalog() -> None:
        raise sqlite3.OperationalError("private-database-path-marker")

    return app


@pytest.fixture
def proxy_factory():
    def build(upstream: httpx.AsyncClient, gateway_id: str) -> FastAPI:
        app = FastAPI()
        app.state.registry = SimpleNamespace(gateway_id=gateway_id)
        app.add_middleware(TraceMiddleware, component="gateway")

        @app.get("/catalog")
        async def catalog(request: Request) -> Response:
            forwarded = upstream.build_request("GET", "/catalog", headers={
                "X-Request-ID": get_request_id(request),
                "X-BoxTeam-Target-Gateway-Id": GATEWAY_B,
                "X-BoxTeam-Workspace-Id": WORKSPACE_ID,
            })
            response = await send_upstream_request(upstream, forwarded)
            try:
                return Response(await response.aread(), status_code=response.status_code,
                                headers=filter_hop_by_hop_headers(response.headers.items()))
            finally:
                await response.aclose()

        return app
    return build


@pytest.mark.parametrize("failure_at", ["backend", "gateway-B", "gateway-A"])
async def test_two_hop_failure_keeps_origin_and_request_identity(backend, proxy_factory, failure_at):
    async def disconnected(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private-upstream-address-marker", request=request)

    async with AsyncExitStack() as stack:
        backend_transport = httpx.MockTransport(disconnected) if failure_at == "gateway-B" else httpx.ASGITransport(backend)
        backend_client = await stack.enter_async_context(httpx.AsyncClient(transport=backend_transport, base_url="http://backend.test"))
        gateway_b = proxy_factory(backend_client, GATEWAY_B)
        remote_transport = httpx.MockTransport(disconnected) if failure_at == "gateway-A" else httpx.ASGITransport(gateway_b)
        remote_client = await stack.enter_async_context(httpx.AsyncClient(transport=remote_transport, base_url="http://gateway-b.test"))
        gateway_a = proxy_factory(remote_client, GATEWAY_A)
        browser = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(gateway_a), base_url="http://gateway-a.test"))
        response = await browser.get("/catalog", headers={"X-Request-ID": "req-two-hop"})

    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "req-two-hop"
    context = json.loads(response.headers[HTTP_FAILURE_HEADER])
    assert "private-" not in response.text + response.headers[HTTP_FAILURE_HEADER]
    if failure_at == "backend":
        assert response.status_code == 500
        assert context["source"] == {"component": "workspace_backend", "backend_id": "backend-B2", "workspace_id": WORKSPACE_ID}
        assert context["error_type"] == "OperationalError"
    else:
        assert response.status_code == 502
        assert context["source"] == {"component": "gateway", "gateway_id": GATEWAY_A if failure_at == "gateway-A" else GATEWAY_B}
        assert context["target"] == {"gateway_id": GATEWAY_B, "workspace_id": WORKSPACE_ID}
        assert context["stage"] == "upstream_request"


async def test_cancelled_upstream_is_not_reported_as_transport_failure():
    import asyncio

    cancellation = asyncio.CancelledError("用户取消")

    async def cancel(request: httpx.Request) -> httpx.Response:
        raise cancellation

    async with httpx.AsyncClient(transport=httpx.MockTransport(cancel)) as client:
        with pytest.raises(asyncio.CancelledError) as caught:
            await send_upstream_request(client, client.build_request("GET", "http://backend.test/catalog"))
    assert caught.value is cancellation


async def test_plain_upstream_error_is_not_attributed_to_current_gateway():
    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "service unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        response = await send_upstream_request(client, client.build_request("GET", "http://service.test/catalog"))
        try:
            assert response.status_code == 503
            assert json.loads(response.headers[HTTP_FAILURE_HEADER]) == {
                "source": {"component": "unknown_upstream"}, "stage": "upstream_response",
            }
            assert json.loads(await response.aread()) == {"detail": "service unavailable"}
        finally:
            await response.aclose()


async def test_relayed_lowercase_origin_header_is_not_duplicated():
    context = {"source": {"component": "workspace_backend", "backend_id": "backend-B2"}, "stage": "http_request"}
    app = FastAPI()
    app.add_middleware(TraceMiddleware, component="gateway")
    app.exception_handler(HTTPException)(gateway_http_exception_handler)

    @app.get("/catalog")
    async def catalog() -> None:
        raise HTTPException(500, "原始业务错误", headers={HTTP_FAILURE_HEADER.lower(): json.dumps(context)})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway.test") as client:
        response = await client.get("/catalog")
    assert len(response.headers.get_list(HTTP_FAILURE_HEADER)) == 1
    assert json.loads(response.headers[HTTP_FAILURE_HEADER]) == context
    assert response.json()["detail"] == "原始业务错误"
