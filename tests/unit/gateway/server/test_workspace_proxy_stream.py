from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.server.workspace_proxy import (
    _stream_proxy_body,
    _stream_proxy_response,
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
