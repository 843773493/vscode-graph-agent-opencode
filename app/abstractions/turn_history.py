from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Literal, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field

from app.schemas.event import Event
from app.schemas.public_v2.common import JobStatus
from app.schemas.public_v2.message import MessageDTO
from app.schemas.public_v2.session import SessionDTO
from app.schemas.public_v2.trace import TraceEventDTO
from app.schemas.public_v2.turn import (
    TurnDetailBatchDTO,
    TurnDetailDTO,
    TurnPageDTO,
    TurnSummaryDTO,
    TurnUserMessageDTO,
)


class TurnProjectionPatch(BaseModel):
    """单个语义事件对既有 Turn 的线性空间增量。"""

    revision: int = Field(ge=2)
    updated_at: datetime
    status: JobStatus | None = None
    completed_at: datetime | None = None
    source_message_ids: list[str] | None = None
    merged_job_ids: list[str] | None = None
    user_messages: list[TurnUserMessageDTO] | None = None
    response_preview: str | None = Field(default=None, max_length=1000)
    preview_truncated: bool | None = None
    final_response: str | None = None
    append_items: list[TraceEventDTO] = Field(default_factory=list)


class TurnProjectionMutation(BaseModel):
    turn_id: str
    base_revision: int = Field(ge=0)
    create: TurnDetailDTO | None = None
    patch: TurnProjectionPatch | None = None


class TurnProjectionOperation(BaseModel):
    schema_version: Literal[1] = 1
    event_id: str
    source_event_offset: int | None = Field(default=None, ge=1)
    mutations: list[TurnProjectionMutation] = Field(default_factory=list)
    hidden_turn_ids: list[str] = Field(default_factory=list)


class TurnProjectionPublicationConflict(RuntimeError):
    """staging 发布时权威事件水位已变化。"""


class TurnProjectionWatermark(BaseModel):
    event_id: str | None = None
    source_offset: int = Field(default=0, ge=0)
    projection_epoch: int = Field(ge=1)


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


@runtime_checkable
class TurnLegacyMessageSourceProtocol(Protocol):
    def has_checkpoint_history(self, session_id: str) -> bool: ...

    async def list_visible_messages_for_turn_migration(
        self,
        session_id: str,
    ) -> list[MessageDTO]: ...


@runtime_checkable
class TurnHistoryStoreProtocol(Protocol):
    def apply_operation(
        self,
        session_id: str,
        operation: TurnProjectionOperation,
    ) -> bool: ...

    def get_turn(self, session_id: str, turn_id: str) -> TurnDetailDTO | None: ...

    def is_event_applied(
        self,
        session_id: str,
        turn_id: str,
        event_id: str,
    ) -> bool: ...

    def get_details(
        self,
        session_id: str,
        turn_ids: list[str],
    ) -> TurnDetailBatchDTO: ...

    def latest_summary(self, session_id: str) -> TurnSummaryDTO | None: ...

    def list_summaries(
        self,
        session_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> TurnPageDTO: ...

    def projection_epoch(self, session_id: str) -> int: ...

    def publication_watermark(self, session_id: str) -> TurnProjectionWatermark: ...

    def visible_turn_ids_from_message(
        self,
        session_id: str,
        message_id: str,
    ) -> list[str]: ...

    def truncate_from_message(self, session_id: str, message_id: str) -> int: ...

    def projection_version(self, session_id: str) -> int: ...

    def event_cursor(self, session_id: str) -> str | None: ...

    def event_offset(self, session_id: str) -> int: ...

    def advance_event_cursor(
        self,
        session_id: str,
        event_id: str,
        *,
        source_offset: int,
    ) -> None: ...

    def projection_exists(self, session_id: str) -> bool: ...

    def projection_status(self, session_id: str) -> str: ...

    def history_initialized(self, session_id: str) -> bool: ...

    def mark_history_initialized(
        self,
        session_id: str,
        *,
        projection_version: int,
    ) -> None: ...

    def set_projection_status(
        self,
        session_id: str,
        status: Literal["ready", "partial", "failed"],
        *,
        error: str | None = None,
    ) -> None: ...

    def next_ordinal(self, session_id: str) -> int: ...

    def turn_count(self, session_id: str) -> int: ...

    def rebase(self, session_id: str) -> int: ...

    def compact(self, session_id: str) -> None: ...

    def discard_projection(self, session_id: str) -> None: ...

    def publish_staging(
        self,
        session_id: str,
        staging: TurnHistoryStoreProtocol,
        *,
        publication_base: TurnProjectionWatermark | None = None,
    ) -> int: ...

    def create_rebuild_staging(self, session_id: str) -> TurnHistoryStoreProtocol: ...


@runtime_checkable
class TurnEventProjectorProtocol(Protocol):
    def record_event(
        self,
        session_id: str,
        event: Event,
        *,
        source_offset: int | None = None,
    ) -> TurnDetailDTO | None: ...

    def rebuild_from_events(
        self,
        session_id: str,
        events: list[Event],
        *,
        destructive: bool = False,
    ) -> int: ...

    def synchronize(
        self,
        session_id: str,
        operation: Callable[[], _ResultT],
    ) -> _ResultT: ...


_ResultT = TypeVar("_ResultT")


@runtime_checkable
class TurnProjectorFactoryProtocol(Protocol):
    def __call__(
        self,
        store: TurnHistoryStoreProtocol,
    ) -> TurnEventProjectorProtocol: ...
