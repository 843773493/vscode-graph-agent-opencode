from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class BackgroundMessageKind(str, Enum):
    normal = "normal"
    interrupt = "interrupt"


class BackgroundMessageDTO(BaseModel):
    message_id: str
    session_id: str
    agent_id: str
    source_id: str
    kind: BackgroundMessageKind
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime


class BackgroundMessageBatchDTO(BaseModel):
    session_id: str
    agent_id: str
    source_id: str | None = None
    messages: list[BackgroundMessageDTO] = Field(default_factory=list)
    interrupted: bool = False
    timed_out: bool = False
    last_message_id: str | None = None
    collected_at: datetime


@runtime_checkable
class BackgroundMessageBusProtocol(Protocol):
    def emit(
        self,
        session_id: str,
        agent_id: str,
        content: str,
        *,
        kind: BackgroundMessageKind | str = BackgroundMessageKind.normal,
        source_id: str | None = None,
        payload: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> BackgroundMessageDTO: ...

    async def collect(
        self,
        session_id: str,
        agent_id: str,
        *,
        source_id: str | None = None,
        after_message_id: str | None = None,
        timeout_seconds: int = 300,
        poll_interval_seconds: float = 1.0,
        stop_on_interrupt: bool = True,
    ) -> BackgroundMessageBatchDTO: ...
