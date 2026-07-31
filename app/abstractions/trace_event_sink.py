from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from app.schemas.event import Event


class TraceAppendReceipt(BaseModel):
    event_id: str
    trace_end_offset: int = Field(ge=1)
    projected_event_offset: int | None = Field(default=None, ge=1)


class TraceEventSinkProtocol(Protocol):
    """会话 trace 事件的持久化写入边界。"""

    async def append(self, session_id: str, event: Event) -> TraceAppendReceipt: ...
