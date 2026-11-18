"""持久任务的累计用量、执行归属与投递水位；正文仍归 canonical owner。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.abstractions.internal_message import PreparedInternalMessage


class TaskUsageDTO(BaseModel):
    tokens_used: int = Field(default=0, ge=0)
    time_used_seconds: int = Field(default=0, ge=0)


class TaskExecutionReferenceDTO(BaseModel, frozen=True):
    session_id: str
    thread_id: str
    execution_id: str
    started_at: datetime
    control_revision: int
    run_id: str | None = None
    role: Literal["work", "evaluation", "report", "notice"] = "work"


class TaskJobAccountingDTO(BaseModel):
    tokens: int = Field(default=0, ge=0)
    elapsed_seconds: int = Field(default=0, ge=0)
    time_closed: bool = False
    execution: TaskExecutionReferenceDTO | None = None
    settled: bool = False


class TaskDispatchDTO(BaseModel):
    trigger_id: str
    idempotency_key: str
    source_id: str
    revision: int
    control_revision: int
    message: PreparedInternalMessage | None = None
    job_id: str | None = None


class TaskUsageStateDTO(TaskUsageDTO):
    sync_version: int = 0
    last_accounted_job_id: str | None = None
    accounted_jobs: dict[str, TaskJobAccountingDTO] = Field(default_factory=dict)
    dispatch: TaskDispatchDTO | None = None
    last_continued_job_id: str | None = None
