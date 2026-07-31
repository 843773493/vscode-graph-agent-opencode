from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import JobStatus
from .session import SessionDTO
from .trace import TraceEventDTO

TurnItemsView = Literal["summary", "full"]
TurnCursorDirection = Literal["older"]
TurnProjectionState = Literal["ready", "partial"]


class TurnAttachmentDTO(BaseModel):
    """Turn 展示层只保存持久化附件引用，不携带内联 data URL。"""

    file_id: str
    name: str | None = None
    content_type: str | None = None


class TurnUserMessageDTO(BaseModel):
    """一次执行 Turn 所消费的用户可见消息。"""

    message_id: str
    content: str
    attachments: list[TurnAttachmentDTO] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class TurnUserMessageSummaryDTO(BaseModel):
    message_id: str
    preview: str = Field(default="", max_length=500)
    content_truncated: bool = False
    attachment_count: int = Field(default=0, ge=0)
    created_at: datetime


class TurnBaseDTO(BaseModel):
    """Turn summary 与 detail 共享的稳定身份和状态。"""

    turn_id: str
    job_id: str
    session_id: str
    ordinal: int = Field(ge=1)
    revision: int = Field(ge=1)
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_turn_identity(self) -> TurnBaseDTO:
        if self.turn_id != self.job_id:
            raise ValueError("Turn ID 必须等于实际执行 Job ID")
        return self


class TurnSummaryDTO(TurnBaseDTO):
    items_view: Literal["summary"] = "summary"
    source_message_ids: list[str] = Field(default_factory=list, max_length=32)
    source_message_count: int = Field(default=0, ge=0)
    merged_job_ids: list[str] = Field(default_factory=list, max_length=32)
    merged_job_count: int = Field(default=0, ge=0)
    sources_truncated: bool = False
    user_messages: list[TurnUserMessageSummaryDTO] = Field(
        default_factory=list,
        max_length=8,
    )
    user_message_count: int = Field(default=0, ge=0)
    user_messages_truncated: bool = False
    response_preview: str = Field(default="", max_length=1000)
    preview_truncated: bool = False
    item_count: int = Field(default=0, ge=0)


class TurnDetailDTO(TurnBaseDTO):
    items_view: Literal["full"] = "full"
    source_message_ids: list[str] = Field(default_factory=list)
    merged_job_ids: list[str] = Field(default_factory=list)
    user_messages: list[TurnUserMessageDTO] = Field(default_factory=list)
    response_preview: str = Field(default="", max_length=1000)
    preview_truncated: bool = False
    final_response: str = ""
    items: list[TraceEventDTO] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source_identity(self) -> TurnDetailDTO:
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("Turn source_message_ids 不能重复")
        if len(set(self.merged_job_ids)) != len(self.merged_job_ids):
            raise ValueError("Turn merged_job_ids 不能重复")
        return self


class TurnPageDTO(BaseModel):
    items: list[TurnSummaryDTO] = Field(max_length=20)
    next_cursor: str | None = None
    has_more: bool = False
    projection_epoch: int = Field(ge=1)


class TurnDetailBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_ids: list[str] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def validate_unique_turn_ids(self) -> TurnDetailBatchRequest:
        if len(set(self.turn_ids)) != len(self.turn_ids):
            raise ValueError("turn_ids 不能重复")
        return self


class TurnDetailBatchDTO(BaseModel):
    items: list[TurnDetailDTO] = Field(max_length=4)
    projection_epoch: int = Field(ge=1)


class TurnJobSummaryDTO(BaseModel):
    job_id: str
    message_id: str
    status: JobStatus
    updated_at: datetime


class SessionTurnBootstrapDTO(BaseModel):
    session: SessionDTO
    latest_turn: TurnSummaryDTO | None = None
    active_job_id: str | None = None
    active_jobs: list[TurnJobSummaryDTO] = Field(default_factory=list, max_length=8)
    active_job_count: int = Field(default=0, ge=0)
    active_jobs_truncated: bool = False
    projection_state: TurnProjectionState = "ready"
    older_cursor: str | None = None
    event_cursor: str | None = None
    projection_epoch: int = Field(ge=1)


class TurnCursorDTO(BaseModel):
    """服务端编码进不透明 cursor 的稳定锚点。"""

    version: Literal[1] = 1
    session_id: str
    projection_epoch: int = Field(ge=1)
    anchor_turn_id: str
    include_anchor: bool = False
    direction: TurnCursorDirection = "older"


class StaleTurnCursorErrorDTO(BaseModel):
    code: Literal["stale_turn_cursor"] = "stale_turn_cursor"
    session_id: str
    cursor_epoch: int = Field(ge=1)
    current_epoch: int = Field(ge=1)
    message: str


class TurnProjectionCorruptedErrorDTO(BaseModel):
    code: Literal["turn_projection_corrupted"] = "turn_projection_corrupted"
    session_id: str
    message: str
