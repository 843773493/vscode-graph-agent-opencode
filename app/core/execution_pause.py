"""原协程的进程内执行准入与扣除静止暂停时间的时钟。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from time import monotonic
from typing import Literal


class ExecutionPause:
    def __init__(self) -> None:
        self.requested = False
        self.active_count = 0
        self._paused_at: float | None = None
        self._paused_seconds = 0.0
        self._changed = asyncio.Event()

    @property
    def state(self) -> Literal["running", "pausing", "paused"]:
        if not self.requested:
            return "running"
        return "pausing" if self.active_count else "paused"

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def request(self) -> None:
        if not self.requested:
            self.requested = True
            if not self.active_count:
                self._paused_at = monotonic()
            self._notify()

    def resume(self) -> None:
        if self.requested:
            if self._paused_at is not None:
                self._paused_seconds += monotonic() - self._paused_at
                self._paused_at = None
            self.requested = False
            self._notify()

    def clock(self) -> float:
        now = self._paused_at if self._paused_at is not None else monotonic()
        return now - self._paused_seconds

    async def sleep_active(self, seconds: float) -> None:
        deadline = self.clock() + seconds
        while (remaining := deadline - self.clock()) > 0:
            changed = self._changed
            if self._paused_at is not None:
                await changed.wait()
            else:
                try:
                    await asyncio.wait_for(changed.wait(), timeout=remaining)
                except TimeoutError:
                    pass

    @asynccontextmanager
    async def boundary(self) -> AsyncIterator[None]:
        while self.requested:
            await self._changed.wait()
        self.active_count += 1
        try:
            yield
        finally:
            self.active_count -= 1
            if self.requested and not self.active_count:
                self._paused_at = monotonic()
                self._notify()


_CURRENT_PAUSE: ContextVar[ExecutionPause | None] = ContextVar(
    "boxteam_execution_pause", default=None,
)


def current_execution_pause() -> ExecutionPause | None:
    return _CURRENT_PAUSE.get()


@contextmanager
def bind_execution_pause(pause: ExecutionPause) -> Iterator[None]:
    token = _CURRENT_PAUSE.set(pause)
    try:
        yield
    finally:
        _CURRENT_PAUSE.reset(token)


@asynccontextmanager
async def execution_pause_boundary() -> AsyncIterator[None]:
    pause = current_execution_pause()
    if pause is None:
        yield
    else:
        async with pause.boundary():
            yield
