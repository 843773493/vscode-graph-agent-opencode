from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .common import CursorPage, RunMode, TimestampedDTO


class SessionCreateRequest(BaseModel):
    title: Optional[str] = "新会话"
    agent_id: Optional[str] = None


class SessionUpdateRequest(BaseModel):
    title: Optional[str] = None
    agent_id: Optional[str] = None


class SessionDTO(TimestampedDTO):
    session_id: str
    workspace_id: str
    title: str
    current_agent_id: str


class SessionListResultDTO(BaseModel):
    items: list[SessionDTO]
    total: int
    cursor: Optional[str] = None


class SessionAutoContinueStartRequest(BaseModel):
    poll_interval_seconds: float = 1.0


class SessionAutoContinueStatusDTO(BaseModel):
    session_id: str
    enabled: bool
    task_id: Optional[str]
    task_status: str
    poll_interval_seconds: Optional[float]
    started_at: Optional[datetime]
    forwarded_count: int
    last_forwarded_at: Optional[datetime]
    last_trigger_event_id: Optional[str]
    last_trigger_job_id: Optional[str]
    last_enqueued_job_id: Optional[str]


class DeleteSessionResultDTO(BaseModel):
    session_id: str
    status: str


class SessionControlResultDTO(BaseModel):
    session_id: str
    action: str
    status: str


class SessionInterruptResultDTO(BaseModel):
    session_id: str
    job_id: str
    status: str
    phase: str
    tool_name: Optional[str] = None
    interrupted_at: datetime = Field(default_factory=lambda: datetime.now())


class SessionCompactResultDTO(BaseModel):
    session_id: str
    status: Literal["compacted", "skipped"]
    message: str
    before_message_count: int
    effective_message_count_before: int
    effective_message_count_after: int
    summarized_message_count: int
    retained_message_count: int
    summary: Optional[str] = None
    history_file_path: Optional[str] = None
    compacted_at: datetime = Field(default_factory=lambda: datetime.now())
