from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .attachment import AttachmentRef

DeliveryPolicy = Literal["after_turn", "after_tool_result", "after_interrupt"]
DeliveryBoundary = Literal[
    "idle",
    "after_turn",
    "after_tool_result",
    "after_interrupt",
]
PendingRequestStatus = Literal["queued"]


class PendingRequestDTO(BaseModel):
    """会话 FIFO 队列中的单条用户消息。"""

    job_id: str
    message_id: str
    session_id: str
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    delivery_policy: DeliveryPolicy
    enqueue_sequence: int = Field(ge=1)
    position: int = Field(ge=0)
    status: PendingRequestStatus = "queued"
    waiting_reason: str | None = None
    last_boundary: DeliveryBoundary | None = None
    agent_id: str
    message_created_at: str
    message_metadata: dict[str, object] = Field(default_factory=dict)
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


class PendingRequestUpdateRequest(BaseModel):
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)


class PendingRequestPolicyUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_policy: DeliveryPolicy
    expected_snapshot_version: int | None = Field(default=None, ge=0)


class PendingRequestListDTO(BaseModel):
    session_id: str
    active_job_id: str | None = None
    requests: list[PendingRequestDTO] = Field(default_factory=list)
    snapshot_version: int = Field(default=0, ge=0)


class PendingRequestSummaryDTO(BaseModel):
    job_id: str
    message_id: str
    enqueue_sequence: int = Field(ge=1)
    delivery_policy: DeliveryPolicy
    status: PendingRequestStatus
    updated_at: datetime


class PendingRequestSummaryListDTO(BaseModel):
    session_id: str
    active_job_id: str | None = None
    requests: list[PendingRequestSummaryDTO] = Field(default_factory=list)
    request_count: int = Field(ge=0)
    snapshot_version: int = Field(default=0, ge=0)
    truncated: bool = False
