from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


ToolTestStatus = Literal["queued", "running", "completed", "failed"]


class ToolTestStartRequest(BaseModel):
    agent_id: str = "default"
    provider_ids: list[str] = Field(default_factory=list)
    repetitions: int = Field(default=1, ge=1, le=20)


class ToolTestAttemptDTO(BaseModel):
    attempt_id: str
    case_id: str
    provider_id: str
    model: str
    status: ToolTestStatus
    passed: bool
    tool_called: bool
    execution_succeeded: bool
    duration_ms: int
    detail: str
    model_calls: int = 0
    reasoning_only_calls: int = 0
    transient_retries: int = 0
    error: str | None = None


class ToolTestProviderResultDTO(BaseModel):
    provider_id: str
    model: str
    status: ToolTestStatus
    completed: int = 0
    total: int = 0
    passed: int = 0
    failed: int = 0
    model_calls: int = 0
    reasoning_only_calls: int = 0
    transient_retries: int = 0
    success_rate: float = 0


class ToolTestRunDTO(BaseModel):
    run_id: str
    tool_name: str
    status: ToolTestStatus
    progress: int = 0
    created_at: datetime
    updated_at: datetime
    repetitions: int
    providers: list[ToolTestProviderResultDTO] = Field(default_factory=list)
    attempts: list[ToolTestAttemptDTO] = Field(default_factory=list)
    error: str | None = None


class ToolTestRunListDTO(BaseModel):
    items: list[ToolTestRunDTO] = Field(default_factory=list)
