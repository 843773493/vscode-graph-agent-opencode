from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.schemas.event import Event
from app.schemas.public_v2.session import SessionDTO


class TurnIndexedEvent(BaseModel):
    event: Event
    source_offset: int = Field(ge=1)


class TurnBootstrapBatch(BaseModel):
    """同步 bootstrap 可安全读取的有界事件骨架。"""

    events: list[TurnIndexedEvent] = Field(default_factory=list)
    event_cursor: str | None = None
    event_offset: int | None = Field(default=None, ge=1)
    has_older_events: bool = False
    index_available: bool = True


class TurnRecoveryBatch(BaseModel):
    """Turn cursor 落后 Trace 时的有界补投结果。"""

    events: list[TurnIndexedEvent] = Field(default_factory=list)
    event_cursor: str | None = None
    event_offset: int | None = Field(default=None, ge=1)
    complete: bool = True
    bytes_read: int = Field(default=0, ge=0)


class TurnMigrationSnapshot(BaseModel):
    """一次完整迁移固定读取的 Trace 水位。"""

    message_trace_size: int = Field(ge=0)
    event_cursor: str | None = None
    projected_event_offset: int | None = Field(default=None, ge=1)


@runtime_checkable
class TurnHistoryEventSourceProtocol(Protocol):
    def ensure_turn_index(self, session_id: str) -> None: ...

    def read_turn_bootstrap_batch(
        self,
        session_id: str,
        *,
        max_events: int,
        max_bytes: int,
    ) -> TurnBootstrapBatch: ...

    def read_turn_recovery_batch(
        self,
        session_id: str,
        *,
        after_event_id: str | None,
        max_events: int,
        max_bytes: int,
    ) -> TurnRecoveryBatch: ...

    def read_message_events(
        self,
        session_id: str,
        tail_limit: int | None = None,
    ) -> list[Event]: ...

    def read_events(
        self,
        session_id: str,
        after_event_id: str | None = None,
        tail_limit: int | None = None,
    ) -> list[Event]: ...

    def capture_turn_migration_snapshot(
        self,
        session_id: str,
    ) -> TurnMigrationSnapshot: ...

    def iter_message_events(
        self,
        session_id: str,
        *,
        before_offset: int | None = None,
    ) -> Iterator[Event]: ...


@runtime_checkable
class TurnSessionLookupProtocol(Protocol):
    async def get(self, session_id: str) -> SessionDTO: ...
