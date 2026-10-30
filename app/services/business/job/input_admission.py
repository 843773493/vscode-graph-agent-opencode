"""消息 acceptance、Job 登记与上下文修改共用的精确 Thread 临界区。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class _Admission:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task[object] | None = None
    users: int = 0


class ThreadInputAdmission:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _Admission] = {}

    @asynccontextmanager
    async def hold(self, session_id: str, thread_id: str) -> AsyncIterator[None]:
        if not session_id or not thread_id:
            raise ValueError("Thread 准入必须指定 Session 和 Thread")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Thread 准入必须运行在 asyncio Task 中")
        key = (session_id, thread_id)
        entry = self._entries.setdefault(key, _Admission())
        # 比较真实 Task；新任务继承 ContextVar 不能继承锁的所有权。
        if entry.owner is task:
            yield
            return
        entry.users += 1
        try:
            async with entry.lock:
                entry.owner = task
                try:
                    yield
                finally:
                    entry.owner = None
        finally:
            entry.users -= 1
            if not entry.users:
                del self._entries[key]
