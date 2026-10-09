from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from app.core.background_task_contracts import (
    BackgroundMessageBatchDTO,
    BackgroundMessageDTO,
    BackgroundMessageKind,
    BackgroundToolResultIdentity,
)
from app.domain.itemized.records import CanonicalItemRecord


@runtime_checkable
class BackgroundMessageBusProtocol(Protocol):
    async def emit(
        self,
        session_id: str,
        thread_id: str,
        agent_id: str,
        content: str,
        *,
        kind: BackgroundMessageKind | str = BackgroundMessageKind.normal,
        source_id: str | None = None,
        payload: dict[str, JsonValue] | None = None,
        message_id: str,
    ) -> BackgroundMessageDTO: ...

    async def collect(
        self,
        session_id: str,
        thread_id: str,
        agent_id: str,
        *,
        consumption_id: str,
        source_id: str | None = None,
        after_message_id: str | None = None,
        timeout_seconds: int = 300,
        poll_interval_seconds: float = 1.0,
        stop_on_interrupt: bool = True,
        tool_result_identity: BackgroundToolResultIdentity | None = None,
    ) -> BackgroundMessageBatchDTO: ...

    def acknowledge(self, session_id: str, consumption_id: str) -> None: ...

    def acknowledge_tool_results(self, session_id: str, thread_id: str, items: Sequence[CanonicalItemRecord]) -> None: ...

__all__ = ["BackgroundMessageBatchDTO", "BackgroundMessageBusProtocol", "BackgroundMessageDTO", "BackgroundMessageKind"]
