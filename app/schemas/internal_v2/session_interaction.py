from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .common import JobStatus, StepStatus, TimestampedDTO
from .job import JobDTO, StepDTO
from .message import MessageDTO
from .session import SessionDTO


class QuestionOptionDTO(BaseModel):
    label: str
    description: str
    label_key: str | None = None
    description_key: str | None = None
    mode: str | None = None


class QuestionInfoDTO(BaseModel):
    question: str
    header: str
    options: list[QuestionOptionDTO] = Field(default_factory=list)
    multiple: bool = False
    question_key: str | None = None
    header_key: str | None = None
    custom: bool = False


class QuestionRequestDTO(BaseModel):
    id: str
    session_id: str
    questions: list[QuestionInfoDTO] = Field(default_factory=list)
    blocking: bool = False
    tool: dict[str, str] | None = None


class PermissionRequestDTO(BaseModel):
    id: str
    session_id: str
    permission: str
    patterns: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    always: list[str] = Field(default_factory=list)
    tool: dict[str, str] | None = None


class SessionExecutionSnapshotDTO(TimestampedDTO):
    session: SessionDTO
    message: MessageDTO
    job: JobDTO | None = None
    steps: list[StepDTO] = Field(default_factory=list)
    status: JobStatus = JobStatus.accepted
    active_step_status: StepStatus | None = None
    last_event_id: str | None = None
