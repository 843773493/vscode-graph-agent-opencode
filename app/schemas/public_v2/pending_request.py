from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .attachment import AttachmentRef

DeliveryPolicy = Literal["after_turn", "after_tool_result", "after_interrupt"]
DeliveryBoundary = Literal[
    "idle",
    "after_turn",
    "after_tool_result",
    "after_interrupt",
]
PendingRequestStatus = Literal["queued"]


class PendingRequestDTO(BaseModel):
    """会话 FIFO 队列中的单条用户消息。"""

    job_id: str
    message_id: str
    session_id: str
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    delivery_policy: DeliveryPolicy
    enqueue_sequence: int = Field(ge=1)
    position: int = Field(ge=0)
    status: PendingRequestStatus = "queued"
    waiting_reason: str | None = None
    last_boundary: DeliveryBoundary | None = None
    agent_id: str
    message_created_at: str
    message_metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    snapshot_version: int = Field(ge=0)


class PendingRequestUpdateRequest(BaseModel):
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)


class PendingRequestPolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_policy: DeliveryPolicy
    expected_snapshot_version: int | None = Field(default=None, ge=0)


class PendingRequestListDTO(BaseModel):
    session_id: str
    active_job_id: str | None = None
    requests: list[PendingRequestDTO] = Field(default_factory=list)
    snapshot_version: int = Field(default=0, ge=0)


class PendingRequestSummaryDTO(BaseModel):
    job_id: str
    message_id: str
    enqueue_sequence: int = Field(ge=1)
    delivery_policy: DeliveryPolicy
    status: PendingRequestStatus
    updated_at: datetime


class PendingRequestSummaryListDTO(BaseModel):
    session_id: str
    active_job_id: str | None = None
    requests: list[PendingRequestSummaryDTO] = Field(default_factory=list)
    request_count: int = Field(ge=0)
    snapshot_version: int = Field(default=0, ge=0)
    truncated: bool = False
