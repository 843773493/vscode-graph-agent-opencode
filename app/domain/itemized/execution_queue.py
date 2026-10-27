"""执行队列只保存已准入输入的调度引用，不持有消息正文。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.domain.itemized.execution_admission import (
    ExecutionAdmissionCursor,
    ExecutionBinding,
    InternalExecutionIntent,
    UserExecutionIdentity,
)

DeliveryPolicy = Literal["after_turn", "after_tool_result", "after_interrupt"]
DeliveryBoundary = Literal["idle", "after_turn", "after_tool_result", "after_interrupt"]
ExecutionQueuePhase = Literal["queued", "claimed", "started", "terminal"]


@dataclass(frozen=True, slots=True)
class ExecutionQueueInput:
    """准入事务冻结的调度信息；后续 policy 更新不改写该输入。"""

    execution_binding: ExecutionBinding
    message_id: str
    agent_id: str
    provider_id: str | None
    delivery_policy: DeliveryPolicy
    gateway_id: str | None
    request_id: str | None
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution_binding, (UserExecutionIdentity, InternalExecutionIntent)):
            raise TypeError("队列输入必须携带完整 execution binding")
        for name in ("message_id", "agent_id", "created_at"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"队列输入 {name} 必须是非空字符串")
        for name in ("provider_id", "gateway_id", "request_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"队列输入 {name} 必须是非空字符串或 None")
        if self.delivery_policy not in {"after_turn", "after_tool_result", "after_interrupt"}:
            raise ValueError("队列输入 delivery_policy 非法")
        if isinstance(self.execution_binding, InternalExecutionIntent) and self.execution_binding.message_id != self.message_id:
            raise ValueError("队列输入与 internal message identity 不一致")
        datetime.fromisoformat(self.created_at)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ExecutionQueueInput:
        binding = value.get("execution_binding")
        if not isinstance(binding, dict):
            raise TypeError("队列输入缺少 execution binding")
        identity = UserExecutionIdentity if "turn_id" in binding else InternalExecutionIntent
        return cls(**{**value, "execution_binding": identity(**binding)})


@dataclass(frozen=True, slots=True)
class ExecutionQueueRecord:
    """原 admission cursor 与调度 claim 的只读投影。"""

    cursor: ExecutionAdmissionCursor
    input: ExecutionQueueInput
    delivery_policy: DeliveryPolicy
    phase: ExecutionQueuePhase
    claim_owner: str | None
    claim_generation: int
    updated_at: str
    outcome: str

    def __post_init__(self) -> None:
        if self.delivery_policy not in {"after_turn", "after_tool_result", "after_interrupt"}:
            raise ValueError("执行队列 policy 非法")
        if self.phase not in {"queued", "claimed", "started", "terminal"}:
            raise ValueError("执行队列 phase 非法")
        if not isinstance(self.claim_generation, int) or isinstance(self.claim_generation, bool) or self.claim_generation < 0:
            raise ValueError("执行队列 claim generation 非法")
        if self.phase in {"claimed", "started"} and (not self.claim_owner or self.claim_generation < 1):
            raise ValueError("活动执行队列缺少 claim owner/generation")
        if self.phase == "queued" and (self.claim_owner is not None or self.claim_generation != 0):
            raise ValueError("未领取执行不得携带 claim")
        if (self.phase == "terminal") != (self.outcome != "unknown"):
            raise ValueError("执行队列 phase 与 canonical outcome 不一致")


@dataclass(frozen=True, slots=True)
class ExecutionQueueSnapshot:
    records: tuple[ExecutionQueueRecord, ...]
    revision: int


@dataclass(frozen=True, slots=True)
class ExecutionCompletion:
    """原终态提交的只读引用，不复制执行正文或当前可变状态。"""

    commit_id: int
    execution_id: str
    job_id: str
    outcome: str
    request_id: str | None
    # 遍历按首次结束位置推进，精确终态引用可指向之后的控制修正。
    cursor_commit_id: int


@dataclass(frozen=True, slots=True)
class ExecutionCompletionPage:
    records: tuple[ExecutionCompletion, ...]
    through_commit_id: int
