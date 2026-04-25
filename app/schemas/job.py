from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field, model_validator

from app.schemas.common import RunMode, JobStatus, StepStatus, ControlScope, ControlAction


class JobDTO(BaseModel):
    job_id: str
    session_id: str
    mode: RunMode
    status: JobStatus
    entry_agent: str
    progress: int = 0
    current_step: Optional[str] = None
    error_message: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    ended_at: Optional[datetime] = None


class StepDTO(BaseModel):
    step_id: str
    job_id: str
    parent_step_id: Optional[str] = None
    agent_id: Optional[str] = None
    step_type: str
    status: StepStatus
    input_payload: dict[str, Any] = Field(default_factory=dict)
    output_payload: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class JobControlRequest(BaseModel):
    scope: ControlScope = ControlScope.job
    action: ControlAction
    agent_id: Optional[str] = None
    step_id: Optional[str] = None
    message: Optional[str] = None
    input: dict[str, Any] = Field(default_factory=dict)

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
