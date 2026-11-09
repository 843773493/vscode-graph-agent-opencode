"""真实 SDK 读循环关闭必须结清并发请求，不能遗留无限等待者。"""

import asyncio

import anyio
import pytest
from mcp import types
from mcp.shared.message import SessionMessage

from app.services.infrastructure.mcp.invocation import CancellableMcpClientSession


@pytest.fixture
def transport_streams():
    incoming, read = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write, outgoing = anyio.create_memory_object_stream[SessionMessage](0)
    return incoming, read, write, outgoing


async def test_sdk_shutdown_releases_all_pending_requests(transport_streams) -> None:
    incoming, read, write, outgoing = transport_streams
    requests: list[asyncio.Task] = []
    async with incoming, outgoing:
        async with CancellableMcpClientSession(read, write) as session:
            requests = [asyncio.create_task(session.list_tools()), asyncio.create_task(session.call_tool("probe", {}))]
            sent = [await outgoing.receive(), await outgoing.receive()]
            assert {message.message.root.method for message in sent} == {"tools/list", "tools/call"}
            assert len(session._response_streams) == 2
        results = await asyncio.wait_for(asyncio.gather(*requests, return_exceptions=True), 1)
    assert all(isinstance(result, anyio.EndOfStream) for result in results), results
    assert not session._response_streams


async def test_shutdown_keeps_an_already_buffered_result(transport_streams) -> None:
    incoming, read, write, outgoing = transport_streams
    async with incoming, outgoing:
        async with CancellableMcpClientSession(read, write) as session:
            request = asyncio.create_task(session.list_tools())
            message = await outgoing.receive()
            assert isinstance(message.message.root, types.JSONRPCRequest)
            # 将 SDK 已解析的真实结果放入它自己的响应通道，再取消读循环。
            session._response_streams[message.message.root.id].send_nowait(types.JSONRPCResponse(
                jsonrpc="2.0", id=message.message.root.id, result={"tools": []},
            ))
        result = await asyncio.wait_for(request, 1)
    assert result.tools == []
