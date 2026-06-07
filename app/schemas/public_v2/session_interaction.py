from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .common import JobStatus, StepStatus, TimestampedDTO
from .job import JobDTO, StepDTO
from .message import MessageDTO
from .session import SessionDTO


class QuestionOptionDTO(BaseModel):
    label: str
    description: str
    label_key: Optional[str] = None
    description_key: Optional[str] = None
    mode: Optional[str] = None


class QuestionInfoDTO(BaseModel):
    question: str
    header: str
    options: list[QuestionOptionDTO] = Field(default_factory=list)
    multiple: bool = False
    question_key: Optional[str] = None
    header_key: Optional[str] = None
    custom: bool = False


class QuestionRequestDTO(BaseModel):
    id: str
    session_id: str
    questions: list[QuestionInfoDTO] = Field(default_factory=list)
    blocking: bool = False
    tool: Optional[dict[str, str]] = None


class PermissionRequestDTO(BaseModel):
    id: str
    session_id: str
    permission: str
    patterns: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    always: list[str] = Field(default_factory=list)
    tool: Optional[dict[str, str]] = None


class MessageDeltaDTO(BaseModel):
    message_id: str
    part_id: Optional[str] = None
    kind: Literal["text", "reasoning", "tool"]
    delta: str
    final: bool = False
    metadata: dict[str, object] = Field(default_factory=dict)


class JobProgressDTO(BaseModel):
    job_id: str
    status: JobStatus
    current_step_id: Optional[str] = None
    progress: int = 0
    message: Optional[str] = None


class SessionExecutionSnapshotDTO(TimestampedDTO):
    session: SessionDTO
    message: MessageDTO
    job: Optional[JobDTO] = None
    steps: list[StepDTO] = Field(default_factory=list)
    status: JobStatus = JobStatus.accepted
    active_step_status: Optional[StepStatus] = None
    last_event_id: Optional[str] = None


class SessionExecutionEventDTO(BaseModel):
    event_id: str
    session_id: str
    job_id: Optional[str] = None
    type: Literal[
        "message.updated",
        "message.delta",
        "job.updated",
        "job.step.updated",
        "job.status.changed",
        "session.status.changed",
        "session.completed",
        "session.error",
    ]
    time: datetime
    payload: MessageDTO | MessageDeltaDTO | JobDTO | StepDTO | JobProgressDTO | dict[str, object]


class SessionExecutionSseDTO(BaseModel):
    event: SessionExecutionEventDTO
    raw_type: str
    raw_payload: dict[str, object] = Field(default_factory=dict)
