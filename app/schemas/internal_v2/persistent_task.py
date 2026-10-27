"""持久任务的运行时校验；公开传输结构以 workspace public.proto 为准。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.resource_uri import parse_session_thread_uri
from app.schemas.internal_v2.pending_request import DeliveryPolicy

TaskLifecycle = Literal["active", "paused", "complete", "terminated", "needs_attention"]
TaskRunPhase = Literal["pending", "checking", "delivering", "delivered", "checked", "failed", "suppressed"]


class TaskComponentDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"]


class TaskTriggerDTO(TaskComponentDTO):
    type_id: Literal["builtin.manual"]


class TaskConditionDTO(TaskComponentDTO):
    type_id: Literal["builtin.always"]


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


class TaskDecisionDTO(BaseModel):
    decision: Literal["wait", "proceed", "complete", "needs_attention"]
    reason: str


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
