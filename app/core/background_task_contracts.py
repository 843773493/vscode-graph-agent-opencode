"""后台工作与恢复 handler 的唯一纯合同。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class BackgroundMessageKind(str, Enum):
    normal = "normal"
    interrupt = "interrupt"


class BackgroundMessageDTO(BaseModel):
    message_id: str
    session_id: str
    thread_id: str
    agent_id: str
    source_id: str
    kind: BackgroundMessageKind
    content: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    timestamp: datetime


class BackgroundToolResultIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model_call_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)


class BackgroundMessageBatchDTO(BaseModel):
    session_id: str
    thread_id: str
    agent_id: str
    consumption_id: str
    source_id: str | None = None
    messages: list[BackgroundMessageDTO] = Field(default_factory=list)
    interrupted: bool = False
    timed_out: bool = False
    last_message_id: str | None = None
    collected_at: datetime
    tool_result_identity: BackgroundToolResultIdentity | None = None


ACTIVE_TASK_STATUSES = frozenset({"pending", "running", "reconcile_required"})
BackgroundTaskStatus = Literal[
    "pending", "running", "reconcile_required", "completed", "cancelled", "failed", "deleted"
]


class BackgroundTaskDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    version: int = Field(ge=1)
    payload: dict[str, JsonValue]


@dataclass(slots=True)
class BackgroundTaskHandle:
    task_id: str
    session_id: str
    thread_id: str
    agent_id: str
    descriptor: BackgroundTaskDescriptor
    checkpoint: dict[str, JsonValue]
    status: BackgroundTaskStatus
    created_at: datetime
    lease_id: str
    fencing_token: int = 0
    runtime_generation: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    external_effect_pending: bool = False

    @property
    def task_name(self) -> str:
        return self.descriptor.kind

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id, "session_id": self.session_id,
            "thread_id": self.thread_id, "agent_id": self.agent_id,
            "task_name": self.task_name, "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "descriptor": self.descriptor.model_dump(mode="json"),
            "checkpoint": self.checkpoint, "metadata": self.metadata,
            "fencing_token": self.fencing_token,
            "runtime_generation": self.runtime_generation,
            "lease_id": self.lease_id,
            "external_effect_pending": self.external_effect_pending,
        }


@dataclass(frozen=True, slots=True)
class BackgroundTaskStep:
    checkpoint: dict[str, JsonValue]
    completed: bool = False
    messages: tuple[BackgroundMessageDTO, ...] = ()
    result: dict[str, JsonValue] | None = None
    result_message_ids: tuple[str, ...] = ()


class BackgroundTaskHandler(Protocol):
    """每个版本必须校验 descriptor/checkpoint，明确执行副作用能否重放。"""

    kind: str
    version: int
    @property
    def effect(self) -> Literal["atomic_mailbox", "external"]: ...

    def initial_checkpoint(self, descriptor: BackgroundTaskDescriptor) -> dict[str, JsonValue]: ...

    def validate_checkpoint(self, checkpoint: dict[str, JsonValue]) -> None: ...

    async def step(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep: ...

    async def reconcile(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep | None: ...
