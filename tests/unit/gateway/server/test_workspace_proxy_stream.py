from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
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
        runtime=WorkspaceRuntime(
            service_urls={"workspace_api": "http://127.0.0.1:41001"}
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
                runtime=WorkspaceRuntime(
                    service_urls={"workspace_api": "http://127.0.0.1:41002"}
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
