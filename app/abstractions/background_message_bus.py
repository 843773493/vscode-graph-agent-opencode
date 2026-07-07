from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.background_message import (
    BackgroundMessageBatchDTO,
    BackgroundMessageDTO,
    BackgroundMessageKind,
)


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
        payload: dict | None = None,
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
