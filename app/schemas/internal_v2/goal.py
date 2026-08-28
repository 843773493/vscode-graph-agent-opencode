from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

MAX_GOAL_OBJECTIVE_CHARS = 4_000


class GoalStatus(str, Enum):
    active = "active"
    paused = "paused"
    blocked = "blocked"
    usage_limited = "usage_limited"
    budget_limited = "budget_limited"
    complete = "complete"


class GoalJobAccountingDTO(BaseModel):
    tokens: int = 0
    elapsed_seconds: int = 0
    time_closed: bool = False


class SessionGoalDTO(BaseModel):
    goal_id: str
    session_id: str
    objective: str
    status: GoalStatus
    token_budget: int | None = None
    tokens_used: int = 0
    time_used_seconds: int = 0
    revision: int = 0
    created_at: datetime
    updated_at: datetime
    last_accounted_job_id: str | None = None
    last_continued_job_id: str | None = None
    accounted_jobs: dict[str, GoalJobAccountingDTO] = Field(default_factory=dict)


class SessionGoalSetRequest(BaseModel):
    objective: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_GOAL_OBJECTIVE_CHARS,
    )
    status: GoalStatus | None = None
    token_budget: int | None = Field(default=None, ge=1)
    replace: bool = False


class SessionGoalClearResultDTO(BaseModel):
    session_id: str
    cleared: bool
