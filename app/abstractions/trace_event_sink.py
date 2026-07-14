from __future__ import annotations

from typing import Protocol

from app.schemas.event import Event


class TraceEventSinkProtocol(Protocol):
    """会话 trace 事件的持久化写入边界。"""

    async def append(self, session_id: str, event: Event) -> None: ...
