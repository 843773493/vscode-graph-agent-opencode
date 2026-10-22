from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .common import (
    ControlAction,
    ControlScope,
    JobStatus,
    RunMode,
    StepStatus,
    TimestampedDTO,
)
from .pending_request import DeliveryPolicy

JobDispatchStatus = Literal["queued", "running", "completed", "failed", "cancelled", "timed_out"]


class JobDispatchSnapshotDTO(BaseModel):
    """JobService 在调度锁内生成的目标会话队列快照。"""

    session_id: str
    thread_id: str
    job_id: str
    job_status: JobDispatchStatus
    active_job_id: str | None = None
    blocked_by_job_id: str | None = None
    queued_jobs_ahead: int = Field(ge=0)
    queued_job_count: int = Field(ge=0)
    pending_job_count: int = Field(ge=0)
    delivery_policy: DeliveryPolicy | None = None
    enqueue_sequence: int | None = Field(default=None, ge=1)
    queue_snapshot_version: int = Field(default=0, ge=0)


class JobDTO(TimestampedDTO):
    job_id: str
    message_id: str
    session_id: str
    thread_id: str
    mode: RunMode
    status: JobStatus
    entry_agent: str
    progress: int = 0
    current_step: str | None = None
    error_message: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    ended_at: datetime | None = None


class StepDTO(TimestampedDTO):
    step_id: str
    job_id: str
    parent_step_id: str | None = None
    agent_id: str | None = None
    step_type: str
    status: StepStatus
    input_payload: dict[str, object] = Field(default_factory=dict)
    output_payload: dict[str, object] = Field(default_factory=dict)
    started_at: datetime | None = None
    ended_at: datetime | None = None


class JobControlRequest(BaseModel):
    scope: ControlScope = ControlScope.job
    action: ControlAction
    agent_id: str | None = None
    step_id: str | None = None
    message: str | None = None
    params: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scope_target(self):
        if self.scope == ControlScope.agent and not self.agent_id:
            raise ValueError("agent scope requires agent_id")
        if self.scope == ControlScope.step and not self.step_id:
            raise ValueError("step scope requires step_id")
        if self.action in {ControlAction.replace_instruction, ControlAction.append_instruction} and not self.message:
            raise ValueError("instruction action requires message")
        return self


class JobControlResponseDTO(BaseModel):
    job_id: str
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: JobStatus
    control_state: str
