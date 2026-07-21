from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from .common import ControlAction, ControlScope, JobStatus, RunMode, StepStatus, TimestampedDTO
from .pending_request import PendingRequestKind


JobDispatchStatus = Literal["queued", "running"]


class JobDispatchSnapshotDTO(BaseModel):
    """JobService 在调度锁内生成的目标会话队列快照。"""

    session_id: str
    job_id: str
    job_status: JobDispatchStatus
    active_job_id: str
    blocked_by_job_id: Optional[str] = None
    queued_jobs_ahead: int = Field(ge=0)
    queued_job_count: int = Field(ge=0)
    pending_job_count: int = Field(ge=1)
    pending_kind: PendingRequestKind | None = None


class JobDTO(TimestampedDTO):
    job_id: str
    message_id: str
    session_id: str
    mode: RunMode
    status: JobStatus
    entry_agent: str
    progress: int = 0
    current_step: Optional[str] = None
    error_message: Optional[str] = None
    metadata: dict[str, object] = Field(default_factory=dict)
    ended_at: Optional[datetime] = None


class StepDTO(TimestampedDTO):
    step_id: str
    job_id: str
    parent_step_id: Optional[str] = None
    agent_id: Optional[str] = None
    step_type: str
    status: StepStatus
    input_payload: dict[str, object] = Field(default_factory=dict)
    output_payload: dict[str, object] = Field(default_factory=dict)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class JobControlRequest(BaseModel):
    scope: ControlScope = ControlScope.job
    action: ControlAction
    agent_id: Optional[str] = None
    step_id: Optional[str] = None
    message: Optional[str] = None
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
    status: JobStatus
    control_state: str
