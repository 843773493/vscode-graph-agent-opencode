from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.itemized.execution_admission import (
    ExecutionBinding,
    InternalExecutionIntent,
    UserExecutionIdentity,
)

from .attachment import AttachmentRef

DeliveryPolicy = Literal["after_turn", "after_tool_result", "after_interrupt"]
DeliveryBoundary = Literal[
    "idle",
    "after_turn",
    "after_tool_result",
    "after_interrupt",
]
PendingRequestStatus = Literal["queued"]


class PendingRequestControl(BaseModel):
    """FIFO 只持久化调度信息与已接受输入引用。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    execution_binding: ExecutionBinding
    message_id: str
    session_id: str
    thread_id: str
    # 用户执行关联真实 turn_id；内部执行显式为 None。必填以区分缺失和空值。
    turn_id: str | None
    delivery_policy: DeliveryPolicy
    enqueue_sequence: int = Field(ge=1)
    position: int = Field(ge=0)
    status: PendingRequestStatus = "queued"
    waiting_reason: str | None = None
    last_boundary: DeliveryBoundary | None = None
    agent_id: str
    created_at: datetime
    updated_at: datetime
    snapshot_version: int = Field(ge=0)
    # 队列持久化时随 job 携带的请求级真实 gateway_id；缺失 gateway 身份时显式为 None。
    # 无默认值即必填：老数据缺该键会在 model_validate 处 ValidationError（诚实失败），
    # 绝不与「合法但为空」的 None 混淆，也不用字面量或进程级单例补齐。
    gateway_id: str | None
    # 创建该 Job 的权威 request_id（``X-Request-ID``）：job 是独立执行根，
    # 重启恢复必须逐字沿用同一值，MUST NOT 在 job 内部补造第二个请求 ID。
    # 同样无默认值即必填：老数据缺该键即 ValidationError（诚实失败）。
    request_id: str | None

    @model_validator(mode="after")
    def validate_execution_binding(self) -> PendingRequestControl:
        binding = self.execution_binding
        if isinstance(binding, InternalExecutionIntent):
            if binding.message_id != self.message_id:
                raise ValueError("internal Job execution intent 与消息 identity 不一致")
            if self.turn_id is not None:
                raise ValueError("internal pending 不得绑定 Turn")
        else:
            if not isinstance(binding, UserExecutionIdentity):
                raise TypeError("pending execution_binding 必须是已知的 typed binding")
            if binding.job_id != self.job_id:
                raise ValueError("pending Job ID 与 user execution binding 不一致")
            if binding.turn_id != self.turn_id:
                raise ValueError("pending Turn ID 与 user execution binding 不一致")
        return self


class PendingRequestDTO(PendingRequestControl):
    """正文与附件只由已提交输入的展示投影提供，不是队列存储事实。"""

    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    message_created_at: str
    message_metadata: dict[str, object] = Field(default_factory=dict)


class PendingRequestPolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_policy: DeliveryPolicy
    expected_snapshot_version: int | None = Field(default=None, ge=0)


class PendingRequestListDTO(BaseModel):
    session_id: str
    # None 表示整个 Session；精确读取显式返回实际 thread 身份。
    thread_id: str | None = None
    active_job_id: str | None = None
    requests: list[PendingRequestDTO] = Field(default_factory=list)
    snapshot_version: int = Field(default=0, ge=0)


class PendingRequestSummaryDTO(BaseModel):
    job_id: str
    message_id: str
    thread_id: str
    enqueue_sequence: int = Field(ge=1)
    delivery_policy: DeliveryPolicy
    status: PendingRequestStatus
    updated_at: datetime


class PendingRequestSummaryListDTO(BaseModel):
    session_id: str
    thread_id: str | None = None
    active_job_id: str | None = None
    requests: list[PendingRequestSummaryDTO] = Field(default_factory=list)
    request_count: int = Field(ge=0)
    snapshot_version: int = Field(default=0, ge=0)
    truncated: bool = False
