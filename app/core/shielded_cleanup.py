"""在取消已经发生的情况下把清理跑完的统一原语。

uvicorn 声明 ASGI spec 2.3，starlette 的 ``StreamingResponse`` 用 anyio task group
驱动响应体迭代；客户端断开时 cancel scope 取消流任务，此后任意生成器 ``finally``
里的每个 ``await``（获取锁、关闭上游连接、pull 引用计数归零、``aclose`` 源迭代器）
都会立刻重新抛出 ``CancelledError``。清理一旦中途丢失，订阅者/监视任务/上游连接
就会永久泄漏。

Gateway 的两个代理边界与工作区后端多条 SSE 源都需要同一保护，收敛在此单点：

- :func:`run_cleanup_shielded`：把清理放进独立 task 并反复 shield，直到它真正完成。
- :func:`shielded_aclosing`：``contextlib.aclosing`` 的屏蔽版本，折叠整套清理。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from types import TracebackType


async def run_cleanup_shielded(cleanup: Callable[[], Awaitable[None]]) -> None:
    """在取消已经发生的情况下把清理跑完，再由调用方继续传播取消。

    这里把清理放进独立 task 并反复 shield 它直到真正完成。每次取消只能打断
    ``await``，打不断那个独立 task；清理自身失败会在此响亮抛出，不静默吞掉。
    """

    cleanup_task = asyncio.ensure_future(cleanup())
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            continue
    cleanup_task.result()


class shielded_aclosing(AsyncIterator):
    """等价于 ``contextlib.aclosing``，但 ``aclose`` 在取消下也必定跑完。"""

    def __init__(self, thing: AsyncIterator) -> None:
        self.thing = thing

    async def __aenter__(self) -> AsyncIterator:
        return self.thing

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        await run_cleanup_shielded(self.thing.aclose)
