"""持久任务的运行时校验；公开传输结构以 workspace public.proto 为准。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.core.resource_uri import parse_session_thread_uri
from app.core.time_schedule import MisfirePolicy, TimeSchedule
from app.schemas.internal_v2.pending_request import DeliveryPolicy

TaskLifecycle = Literal["active", "paused", "complete", "terminated", "needs_attention"]
TaskRunPhase = Literal["pending", "checking", "delivering", "delivered", "checked", "failed", "suppressed", "skipped"]


class TaskComponentDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"]


class TaskTriggerDTO(TaskComponentDTO):
    type_id: Literal["builtin.manual", "builtin.interval", "builtin.cron"]
    interval_seconds: int | None = Field(default=None, ge=1)
    expression: str | None = None
    timezone: str = "UTC"

    def schedule(self) -> TimeSchedule | None:
        if self.type_id == "builtin.manual":
            return None
        kind = "interval" if self.type_id == "builtin.interval" else "cron"
        return TimeSchedule(kind, self.interval_seconds, self.expression, self.timezone)

    @model_validator(mode="after")
    def validate_schedule(self) -> TaskTriggerDTO:
        if self.type_id == "builtin.manual" and (self.interval_seconds is not None or self.expression is not None or self.timezone != "UTC"):
            raise ValueError("手动触发不能配置时间计划")
        if self.type_id == "builtin.interval" and self.expression is not None:
            raise ValueError("interval trigger 不能配置 cron expression")
        if self.type_id == "builtin.cron" and self.interval_seconds is not None:
            raise ValueError("cron trigger 不能配置 interval_seconds")
        self.schedule()
        return self


class TaskScriptCheckDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    command: list[str] = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    output_limit_bytes: int = Field(gt=0)
    memory_limit_mb: int = Field(ge=16)

    @model_validator(mode="after")
    def validate_command(self) -> TaskScriptCheckDTO:
        if not self.command[0] or any("\0" in argument for argument in self.command):
            raise ValueError("脚本命令需要非空程序且参数不能包含NUL")
        return self


class TaskConditionDTO(TaskComponentDTO):
    type_id: Literal["builtin.always", "builtin.script"]
    script: TaskScriptCheckDTO | None = None

    @model_validator(mode="after")
    def validate_component(self) -> TaskConditionDTO:
        if (self.type_id == "builtin.script") != (self.script is not None):
            raise ValueError("只有script判断必须配置script参数，always不能配置script")
        return self


class TaskContextDTO(TaskComponentDTO):
    type_id: Literal["builtin.literal"]
    content: str = Field(min_length=1)


class TaskActionDTO(TaskComponentDTO):
    type_id: Literal["builtin.runtime_source"]
    target_uri: str
    delivery_mode: Literal["context_only", "context_and_run"]
    delivery_policy: DeliveryPolicy = "after_turn"

    @model_validator(mode="after")
    def require_thread(self) -> TaskActionDTO:
        _resource, _session_id, thread_id = parse_session_thread_uri(self.target_uri)
        if thread_id is None:
            raise ValueError("持久任务动作必须指向明确 Thread URI")
        return self


class TaskPoliciesDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completion: Literal["continuous", "after_delivery"] = "continuous"
    misfire: MisfirePolicy = "skip"
    match_mode: Literal["first_match", "rising_edge", "each_match"] = "each_match"


class PersistentTaskConfigurationDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=200)
    trigger: TaskTriggerDTO
    condition: TaskConditionDTO
    context: TaskContextDTO
    action: TaskActionDTO
    policies: TaskPoliciesDTO = Field(default_factory=TaskPoliciesDTO)


class PersistentTaskDTO(BaseModel):
    task_id: str
    workspace_id: str
    revision: int
    definition_revision: int
    configuration: PersistentTaskConfigurationDTO
    lifecycle: TaskLifecycle
    phase: str
    reason: str | None = None
    recovering: bool = False
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None = None


class TaskDecisionDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    decision: Literal["wait", "proceed", "complete", "needs_attention"]
    reason: str = Field(min_length=1)
    result: dict[str, JsonValue] = Field(default_factory=dict)


class TaskDeliveryDTO(BaseModel):
    state: Literal["saved", "accepted"]
    source_id: str
    item_id: str | None = None
    source_revision: str | None = None
    job_id: str | None = None
    execution_id: str | None = None


class PersistentTaskRunDTO(BaseModel):
    run_id: str
    task_id: str
    task_revision: int
    definition_revision: int
    trigger_key: str
    request_id: str | None = None
    trigger_type: Literal["manual", "interval", "cron"] = "manual"
    scheduled_for: datetime | None = None
    check_only: bool
    phase: TaskRunPhase
    decision: TaskDecisionDTO | None = None
    delivery: TaskDeliveryDTO | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class PersistentTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1)
    configuration: PersistentTaskConfigurationDTO
    enabled: bool = True


class PersistentTaskUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    configuration: PersistentTaskConfigurationDTO


class PersistentTaskControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    action: Literal["pause", "resume", "terminate"]


class PersistentTaskCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1)
    expected_revision: int = Field(ge=1)
    check_only: bool = False


class PersistentTaskListDTO(BaseModel):
    items: list[PersistentTaskDTO]


class PersistentTaskRunListDTO(BaseModel):
    items: list[PersistentTaskRunDTO]
