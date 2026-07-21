from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .attachment import AttachmentRef


PendingRequestKind = Literal["queued", "steering"]


class PendingRequestOrderItem(BaseModel):
    message_id: str
    kind: PendingRequestKind


class PendingRequestDTO(BaseModel):
    """会话中尚未开始执行的用户请求。"""

    job_id: str
    message_id: str
    session_id: str
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    kind: PendingRequestKind
    position: int = Field(ge=0)
    agent_id: str
    message_created_at: str
    message_metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class PendingRequestUpdateRequest(BaseModel):
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)


class PendingRequestReorderRequest(BaseModel):
    requests: list[PendingRequestOrderItem]


class PendingRequestListDTO(BaseModel):
    session_id: str
    active_job_id: str | None = None
    yield_requested: bool = False
    requests: list[PendingRequestDTO] = Field(default_factory=list)
