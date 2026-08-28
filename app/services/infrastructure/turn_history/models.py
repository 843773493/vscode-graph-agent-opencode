from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.internal_v2.turn import (
    StaleTurnCursorErrorDTO,
    StaleTurnReferenceErrorDTO,
    TurnDetailDTO,
    TurnSummaryDTO,
)


class StaleTurnCursorError(RuntimeError):
    def __init__(
        self,
        *,
        session_id: str,
        cursor_epoch: int,
        current_epoch: int,
    ) -> None:
        self.detail = StaleTurnCursorErrorDTO(
            session_id=session_id,
            cursor_epoch=cursor_epoch,
            current_epoch=current_epoch,
            message="Turn 历史已发生破坏性重排，请重新加载最新历史",
        )
        super().__init__(self.detail.message)


class StaleTurnReferenceError(RuntimeError):
    def __init__(self, *, session_id: str, turn_ids: list[str]) -> None:
        self.detail = StaleTurnReferenceErrorDTO(
            session_id=session_id,
            turn_ids=turn_ids,
            message="请求的历史 Turn 已不属于当前上下文，请从当前视图重新加载",
        )
        super().__init__(self.detail.message)


class InvalidTurnCursorError(ValueError):
    pass


class TurnRecord(BaseModel):
    schema_version: Literal[1] = 1
    turn: TurnDetailDTO
    visible: bool = True
    timeline_start: int
    timeline_end: int
    last_applied_event_id: str | None = None


class TurnRecordHeader(BaseModel):
    schema_version: Literal[1] = 1
    summary: TurnSummaryDTO
    visible: bool = True
    timeline_start: int = Field(ge=0)
    timeline_end: int = Field(ge=1)
    last_applied_event_id: str | None = None


class TimelineEntry(BaseModel):
    schema_version: Literal[1] = 1
    turn_id: str
    ordinal: int = Field(ge=1)


class TurnManifest(BaseModel):
    schema_version: Literal[1] = 1
    # TODO: 兼容未记录投影语义版本的既有 manifest；这些文件按 v1 触发重建。
    projection_version: int = Field(default=1, ge=1)
    status: Literal["ready", "partial", "failed"] = "ready"
    projection_epoch: int = Field(default=1, ge=1)
    operation_generation: int = Field(default=1, ge=1)
    applied_operation_offset: int = Field(default=0, ge=0)
    operation_count: int = Field(default=0, ge=0)
    compacted_operation_count: int = Field(default=0, ge=0)
    last_event_id: str | None = None
    last_event_offset: int = Field(default=0, ge=0)
    migration_before_offset: int | None = Field(default=None, ge=0)
    error: str | None = None
    history_initialized: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TurnIndex(BaseModel):
    schema_version: Literal[1] = 1
    projection_epoch: int = Field(default=1, ge=1)
    turn_count: int = Field(default=0, ge=0)
    latest_ordinal: int = Field(default=0, ge=0)
    latest_turn_id: str | None = None
    timeline_size: int = Field(default=0, ge=0)
