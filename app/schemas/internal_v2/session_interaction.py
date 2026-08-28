from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from .common import JobStatus, StepStatus, TimestampedDTO
from .job import JobDTO, StepDTO
from .message import MessageDTO
from .session import SessionDTO
from .session_status import SessionObservationStateDTO, SessionStatusDTO


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


class JobProgressDTO(BaseModel):
    job_id: str
    status: JobStatus
    current_step_id: str | None = None
    progress: int = 0
    message: str | None = None


class SessionExecutionSnapshotDTO(TimestampedDTO):
    session: SessionDTO
    message: MessageDTO
    job: JobDTO | None = None
    steps: list[StepDTO] = Field(default_factory=list)
    status: JobStatus = JobStatus.accepted
    active_step_status: StepStatus | None = None
    last_event_id: str | None = None


class SessionExecutionEventBaseDTO(BaseModel):
    event_id: str
    session_id: str
    job_id: str | None = None
    time: datetime


class MessageObservationDTO(BaseModel):
    message_id: str
    session_id: str
    role: str
    content: str
    attachments: list[object] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class JobStepProgressDTO(BaseModel):
    agent_id: str | None = None
    message: str | None = None
    phase: str | None = None


class SessionErrorPayloadDTO(BaseModel):
    error: str


class TraceObservedPayloadDTO(BaseModel):
    raw_type: str


class MessageUpdatedExecutionEventDTO(SessionExecutionEventBaseDTO):
    type: Literal["message.updated"]
    payload: MessageObservationDTO


class JobUpdatedExecutionEventDTO(SessionExecutionEventBaseDTO):
    type: Literal["job.updated"]
    payload: JobProgressDTO


class JobStepUpdatedExecutionEventDTO(SessionExecutionEventBaseDTO):
    type: Literal["job.step.updated"]
    payload: JobStepProgressDTO


class JobStatusChangedExecutionEventDTO(SessionExecutionEventBaseDTO):
    type: Literal["job.status.changed"]
    payload: JobProgressDTO


class SessionStatusChangedExecutionEventDTO(SessionExecutionEventBaseDTO):
    type: Literal["session.status.changed"]
    payload: SessionStatusDTO | SessionObservationStateDTO


class SessionCompletedExecutionEventDTO(SessionExecutionEventBaseDTO):
    type: Literal["session.completed"]
    payload: JobProgressDTO


class SessionErrorExecutionEventDTO(SessionExecutionEventBaseDTO):
    type: Literal["session.error"]
    payload: SessionErrorPayloadDTO


class TraceObservedExecutionEventDTO(SessionExecutionEventBaseDTO):
    type: Literal["trace.observed"]
    payload: TraceObservedPayloadDTO


SessionExecutionEventDTO = Annotated[
    MessageUpdatedExecutionEventDTO
    | JobUpdatedExecutionEventDTO
    | JobStepUpdatedExecutionEventDTO
    | JobStatusChangedExecutionEventDTO
    | SessionStatusChangedExecutionEventDTO
    | SessionCompletedExecutionEventDTO
    | SessionErrorExecutionEventDTO
    | TraceObservedExecutionEventDTO,
    Field(discriminator="type"),
]


class SessionExecutionSseDTO(BaseModel):
    event: SessionExecutionEventDTO
    raw_type: str
    raw_payload: dict[str, object] = Field(default_factory=dict)
