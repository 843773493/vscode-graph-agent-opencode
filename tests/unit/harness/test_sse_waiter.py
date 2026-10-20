from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable

import httpx
import pytest

from tests.support.sse import read_sse_events_until


@pytest.fixture
async def sse_response() -> AsyncIterator[
    Callable[[AsyncGenerator[bytes, None]], httpx.Response]
]:
    streams: list[tuple[AsyncGenerator[bytes, None], httpx.Response]] = []

    def create(chunks: AsyncGenerator[bytes, None]) -> httpx.Response:
        response = httpx.Response(200, content=chunks)
        streams.append((chunks, response))
        return response

    yield create

    for chunks, response in reversed(streams):
        await chunks.aclose()
        await response.aclose()


@pytest.mark.parametrize("prefix", [b"", b'data: {"type": "unfinished"}'])
async def test_timeout_covers_silent_stream_and_incomplete_line(
    sse_response: Callable[[AsyncGenerator[bytes, None]], httpx.Response],
    prefix: bytes,
) -> None:
    closed = asyncio.Event()

    async def chunks() -> AsyncGenerator[bytes, None]:
        try:
            yield prefix
            await asyncio.Event().wait()
        finally:
            closed.set()

    # 外层期限必须在 raises 之外，避免把测试兜底超时误当成 helper 正确超时。
    async with asyncio.timeout(1):
        with pytest.raises(TimeoutError):
            await read_sse_events_until(
                sse_response(chunks()), lambda event: False, timeout_seconds=0.02,
            )
    assert closed.is_set()


async def test_returns_events_through_match_without_consuming_later_event(
    sse_response: Callable[[AsyncGenerator[bytes, None]], httpx.Response],
) -> None:
    async def chunks() -> AsyncGenerator[bytes, None]:
        yield b': heartbeat\n\nevent: trace\ndata: {"type": "start"}\n\n'
        yield b'data: {"type": "agent_end"}\n\n'
        raise AssertionError("匹配后不应再读取下一条事件")

    events = await read_sse_events_until(
        sse_response(chunks()), lambda event: event.get("type") == "agent_end",
    )
    assert events == [{"type": "start"}, {"type": "agent_end"}]


async def test_finite_stream_returns_events_when_predicate_does_not_match(
    sse_response: Callable[[AsyncGenerator[bytes, None]], httpx.Response],
) -> None:
    async def chunks() -> AsyncGenerator[bytes, None]:
        yield b'data: {"type": "start"}\n\n'

    assert await read_sse_events_until(
        sse_response(chunks()), lambda event: False,
    ) == [{"type": "start"}]


@pytest.mark.parametrize(
    ("body", "error"),
    [(b"{broken", json.JSONDecodeError), (b"[]", TypeError)],
)
async def test_invalid_event_is_reported(
    sse_response: Callable[[AsyncGenerator[bytes, None]], httpx.Response],
    body: bytes,
    error: type[Exception],
) -> None:
    async def chunks() -> AsyncGenerator[bytes, None]:
        yield b"data: " + body + b"\n\n"

    with pytest.raises(error):
        await read_sse_events_until(sse_response(chunks()), lambda event: False)


async def test_caller_cancellation_propagates(
    sse_response: Callable[[AsyncGenerator[bytes, None]], httpx.Response],
) -> None:
    entered = asyncio.Event()

    async def chunks() -> AsyncGenerator[bytes, None]:
        entered.set()
        await asyncio.Event().wait()
        yield b""

    task = asyncio.create_task(read_sse_events_until(
        sse_response(chunks()), lambda event: False,
    ))
    try:
        async with asyncio.timeout(1):
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_transport_failure_propagates(
    sse_response: Callable[[AsyncGenerator[bytes, None]], httpx.Response],
) -> None:
    failure = httpx.ReadError("测试连接读取失败")

    async def chunks() -> AsyncGenerator[bytes, None]:
        raise failure
        yield b""

    with pytest.raises(httpx.ReadError) as captured:
        await read_sse_events_until(sse_response(chunks()), lambda event: False)
    assert captured.value is failure
