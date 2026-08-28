"""为异步上游流提供统一的、可被取消唤醒的读取边界。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from typing import Generic, Self, TypeVar

from app.core.turn_execution_scope import CancellationSignal, ScopeCancelledError

StreamItem = TypeVar("StreamItem")
CloseUpstream = Callable[[], Awaitable[None] | None]


async def _invoke_close(close: CloseUpstream) -> None:
    result = close()
    if inspect.isawaitable(result):
        await result


async def _await_cleanup(cleanup: Awaitable[None]) -> None:
    """让关闭动作脱离当前 task 的一次取消，确保 close 能完成。"""
    cleanup_task = asyncio.ensure_future(cleanup)
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        await asyncio.shield(cleanup_task)
        raise


async def close_async_stream(stream: object) -> None:
    """调用并等待上游异步流的 aclose/close；没有关闭接口时明确为空操作。"""
    close = getattr(stream, "aclose", None)
    if close is None:
        close = getattr(stream, "close", None)
    if close is None:
        return
    if not callable(close):
        raise TypeError(f"异步上游流的 close 成员不可调用: {stream!r}")
    await _await_cleanup(_invoke_close(close))


async def _cancel_and_wait(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def cancelable_next(
    iterator: AsyncIterator[StreamItem],
    signal: CancellationSignal | None,
    *,
    close_upstream_stream: CloseUpstream,
) -> StreamItem:
    """读取一个元素，并保证取消时不遗留挂起的 ``anext`` 任务。"""
    if signal is None:
        return await anext(iterator)

    try:
        signal.raise_if_cancelled()
    except ScopeCancelledError:
        await _await_cleanup(_invoke_close(close_upstream_stream))
        raise

    next_task = asyncio.create_task(anext(iterator))
    cancel_task = asyncio.create_task(signal.wait())
    try:
        done, _pending = await asyncio.wait(
            {next_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done:
            next_task.cancel()
            close_error: BaseException | None = None
            try:
                await _await_cleanup(_invoke_close(close_upstream_stream))
            except BaseException as error:  # noqa: BLE001
                close_error = error
            finally:
                await _cancel_and_wait(next_task)
            if close_error is not None:
                raise close_error
            signal.raise_if_cancelled()
            raise RuntimeError("CancellationSignal 已唤醒但没有取消原因")

        await _cancel_and_wait(cancel_task)
        item = next_task.result()
        try:
            signal.raise_if_cancelled()
        except ScopeCancelledError:
            await _await_cleanup(_invoke_close(close_upstream_stream))
            raise
        return item
    except asyncio.CancelledError:
        next_task.cancel()
        try:
            await _await_cleanup(_invoke_close(close_upstream_stream))
        finally:
            await _cancel_and_wait(next_task)
        raise
    finally:
        await _cancel_and_wait(cancel_task)


class CancelableStream(AsyncIterator[StreamItem], Generic[StreamItem]):
    """封装一个 Provider 异步流，统一处理读取竞速和资源关闭。"""

    def __init__(
        self,
        stream: AsyncIterable[StreamItem],
        signal: CancellationSignal | None,
        *,
        close_upstream_stream: CloseUpstream | None = None,
    ) -> None:
        self._stream = stream
        self._iterator = stream.__aiter__()
        self._signal = signal
        self._close_upstream_stream = close_upstream_stream
        self._closed = False

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamItem:
        return await cancelable_next(
            self._iterator,
            self._signal,
            close_upstream_stream=self.close,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._close_upstream_stream is not None:
            await _await_cleanup(_invoke_close(self._close_upstream_stream))
        else:
            await close_async_stream(self._stream)
