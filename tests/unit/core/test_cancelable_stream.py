from __future__ import annotations

import asyncio

import pytest

from app.core.cancelable_stream import CancelableStream
from app.core.turn_execution_scope import ScopeCancelledError, TurnExecutionScope


class BlockingStream:
    def __init__(self, first_item: str | None = None) -> None:
        self.first_item = first_item
        self.read_started = asyncio.Event()
        self.closed = asyncio.Event()
        self.read_cancelled = False
        self.close_count = 0

    def __aiter__(self) -> BlockingStream:
        return self

    async def __anext__(self) -> str:
        if self.first_item is not None:
            item = self.first_item
            self.first_item = None
            return item
        self.read_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.read_cancelled = True
            raise
        raise AssertionError("测试流不应自行返回第二个元素")

    async def aclose(self) -> None:
        self.close_count += 1
        self.closed.set()


@pytest.mark.asyncio
async def test_cancelable_stream_cancels_pending_read_and_awaits_close() -> None:
    scope = TurnExecutionScope("stream_reader")
    stream = BlockingStream()

    async with CancelableStream(stream, scope.cancellation_signal) as reader:
        read_task = asyncio.create_task(reader.__anext__())
        await stream.read_started.wait()

        assert await scope.cancel("user_requested") is True
        with pytest.raises(ScopeCancelledError, match="user_requested"):
            await read_task

        await stream.closed.wait()
        assert stream.read_cancelled is True
        assert stream.close_count == 1

    await scope.close()


@pytest.mark.asyncio
async def test_cancelable_stream_does_not_interrupt_chunk_processing() -> None:
    scope = TurnExecutionScope("stream_processing")
    stream = BlockingStream(first_item="delta")
    processing_started = asyncio.Event()
    processing_release = asyncio.Event()
    processed: list[str] = []

    async def consume() -> None:
        async with CancelableStream(stream, scope.cancellation_signal) as reader:
            item = await reader.__anext__()
            processing_started.set()
            await processing_release.wait()
            processed.append(item)
            await reader.__anext__()

    consume_task = asyncio.create_task(consume())
    await processing_started.wait()
    assert await scope.cancel("user_requested") is True
    processing_release.set()

    with pytest.raises(ScopeCancelledError, match="user_requested"):
        await consume_task
    assert processed == ["delta"]
    assert stream.close_count == 1
    await scope.close()


@pytest.mark.asyncio
async def test_cancelable_stream_closes_when_reader_task_is_cancelled() -> None:
    scope = TurnExecutionScope("stream_task_cancel")
    stream = BlockingStream()

    async with CancelableStream(stream, scope.cancellation_signal) as reader:
        read_task = asyncio.create_task(reader.__anext__())
        await stream.read_started.wait()
        read_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read_task
        await stream.closed.wait()

    assert stream.read_cancelled is True
    assert stream.close_count == 1
    await scope.close()
