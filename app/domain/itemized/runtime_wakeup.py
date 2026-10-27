"""runtime source 唤醒的稳定引用；正文仅存在 canonical item 中。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeExecutionWakeup:
    session_id: str
    thread_id: str
    source_id: str
    source_item_id: str
    execution_id: str
    job_id: str
    admission_idempotency_key: str
    agent_id: str
    message_created_at: str
    gateway_id: str | None
    request_id: str | None
    lease_id: str
    lifecycle_generation: int
    lease_fencing_token: int
    delivery_policy: str = "after_turn"

    def __post_init__(self) -> None:
        for name in (
            "session_id", "thread_id", "source_id", "source_item_id", "execution_id",
            "job_id", "admission_idempotency_key", "agent_id", "message_created_at",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"RuntimeExecutionWakeup.{name} 必须是非空字符串")
        if self.lifecycle_generation < 1 or self.lease_fencing_token < 1:
            raise ValueError("runtime wakeup lifecycle/fencing generation 必须为正数")
        if self.delivery_policy not in {"after_turn", "after_tool_result", "after_interrupt"}:
            raise ValueError("runtime wakeup delivery_policy 非法")


@dataclass(frozen=True, slots=True)
class RuntimeExecutionProgress:
    """原执行及最终 canonical 消息的只读恢复投影，不保存第二份结果。"""

    outcome: str
    updated_at: str
    total_tokens: int


@dataclass(frozen=True, slots=True)
class RuntimeInputConsumption:
    """输入进入已派发请求的只读证据，不推断 Provider 是否实际收到。"""

    execution_id: str
    model_call_id: str
    assembly_id: str
    request_hash: str
