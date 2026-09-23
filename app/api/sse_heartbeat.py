from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import TypeVar

T = TypeVar("T")

# SSE 空闲心跳的统一口径：trace 流、workspace 文件流、message 流共用 15s 间隔，
# 空闲时发 SSE 注释帧而不是业务事件，客户端断开或源耗尽时立即停止。
SSE_HEARTBEAT_INTERVAL_SECONDS = 15.0
SSE_HEARTBEAT_COMMENT = ": heartbeat\n\n"


async def stream_sse_with_heartbeat(
    source: AsyncIterator[T],
    *,
    heartbeat_interval_seconds: float,
    close_source_on_exit: bool,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[T | str]:
    """把源迭代器包装成带空闲心跳的 SSE 事件流。

    产出源元素本身，或在空闲超时窗口产出 ``SSE_HEARTBEAT_COMMENT`` 注释帧。
    ``is_disconnected`` 非空时在心跳窗口与事件产出前探测客户端断开并立即停止；
    ``close_source_on_exit`` 控制退出时是否额外 ``aclose`` 源迭代器。
    """
    iterator = aiter(source)
    next_item = asyncio.create_task(anext(iterator))
    try:
        while True:
            completed, _ = await asyncio.wait(
                {next_item},
                timeout=heartbeat_interval_seconds,
            )
            if not completed:
                if is_disconnected is not None and await is_disconnected():
                    return
                yield SSE_HEARTBEAT_COMMENT
                continue
            try:
                item = next_item.result()
            except StopAsyncIteration:
                return
            if is_disconnected is not None and await is_disconnected():
                return
            yield item
            next_item = asyncio.create_task(anext(iterator))
    finally:
        if not next_item.done():
            next_item.cancel()
            with suppress(asyncio.CancelledError):
                await next_item
        if close_source_on_exit:
            await iterator.aclose()
