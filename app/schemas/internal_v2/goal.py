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


class SessionGoalStateDTO(BaseModel):
    goal_id: str
    session_id: str
    # Goal 只属于 main thread；该字段是权威 catalog 指针的响应投影，
    # 由服务层解析回填，不写入任务应用状态（避免出现第二 main 指针）。
    thread_id: str
    objective: str
    status: GoalStatus
    token_budget: int | None = None
    tokens_used: int = Field(default=0, description="当前 Goal 的累计 Token；fork 创建独立 Goal 时从 0 开始")
    time_used_seconds: int = Field(default=0, description="当前 Goal 的累计用时；fork 创建独立 Goal 时从 0 开始")
    revision: int = 0
    control_revision: int = 0
    sync_version: int = 0
    created_at: datetime = Field(description="当前 Goal 的创建时刻；fork 的新 Goal 使用目标 Session 冻结创建时刻")
    updated_at: datetime = Field(description="当前 Goal 记录最近一次写入时刻，包含设置、用量记账及续跑标记写入或失败回滚；fork 初值取目标 Session 冻结创建时刻，不继承来源 Goal 时间。它与仅随普通设置更新递增的 revision 是不同维度。")
    last_accounted_job_id: str | None = None
    last_continued_job_id: str | None = None
    accounted_jobs: dict[str, GoalJobAccountingDTO] = Field(default_factory=dict)


class SessionGoalDTO(BaseModel):
    """HTTP、事件和管理读取共用的有界 Goal 展示投影。"""

    goal_id: str
    session_id: str
    thread_id: str
    objective: str
    status: GoalStatus
    token_budget: int | None = None
    tokens_used: int = 0
    time_used_seconds: int = 0
    revision: int = 0
    created_at: datetime
    updated_at: datetime
    control_revision: int = 0
    sync_version: int = 0


def goal_public_view(goal: SessionGoalStateDTO) -> SessionGoalDTO:
    return SessionGoalDTO.model_validate(goal.model_dump(exclude={
        "last_accounted_job_id", "last_continued_job_id", "accounted_jobs",
    }))


class SessionGoalSetRequest(BaseModel):
    objective: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_GOAL_OBJECTIVE_CHARS,
    )
    status: GoalStatus | None = None
    token_budget: int | None = Field(default=None, ge=1)
    replace: bool = False
    operation_id: str = Field(min_length=1)
    expected_control_revision: int = Field(ge=0)


class SessionGoalClearRequest(BaseModel):
    operation_id: str = Field(min_length=1)
    expected_control_revision: int = Field(ge=0)


class SessionGoalSnapshotDTO(BaseModel):
    session_id: str
    thread_id: str
    goal: SessionGoalDTO | None = None
    control_revision: int
    sync_version: int


class SessionGoalOperationResultDTO(BaseModel):
    session_id: str
    thread_id: str
    operation_id: str
    status: str
    control_revision: int
    sync_version: int
    goal: SessionGoalDTO | None = None
