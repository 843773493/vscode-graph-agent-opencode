"""
事件模式的 discriminated union 实现。

每个事件类型都有独立的、带 `type` 字面量字段的 Schema，
使用 Pydantic 的 discriminated union 功能实现类型安全。

参考：kilocode/packages/opencode/src/session/status.ts
"""

from datetime import datetime
from typing import Any, Dict, Literal, Optional, Union
from pydantic import BaseModel, Field


# ============= 1. 基础事件结构（所有事件的公共字段） =============

class BaseEvent(BaseModel):
    """所有事件的基类"""
    event_id: str
    job_id: str
    step_id: Optional[str] = None
    agent_id: Optional[str] = None
    timestamp: datetime


# ============= 2. 各事件的Payload Schema（不含type字段） =============

class MessageCreatedPayload(BaseModel):
    """MESSAGE_CREATED 事件的 payload"""
    message_id: str
    session_id: str
    role: str
    content: str
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class JobCreatedPayload(BaseModel):
    """JOB_CREATED 事件的 payload"""
    session_id: str
    message: str
    agent_id: str


class JobStartedPayload(BaseModel):
    """JOB_STARTED 事件的 payload（无额外字段）"""
    pass


class JobCompletedPayload(BaseModel):
    """JOB_COMPLETED 事件的 payload"""
    result: str = ""


class JobCancelledPayload(BaseModel):
    """JOB_CANCELLED 事件的 payload（无额外字段）"""
    pass


class JobFailedPayload(BaseModel):
    """JOB_FAILED 事件的 payload"""
    error: str


class StatusChangePayload(BaseModel):
    """STATUS_CHANGE 事件的 payload"""
    status: str
    reason: str
    blocked_by_job_id: Optional[str] = None


class AgentStartPayload(BaseModel):
    """AGENT_START 事件的 payload"""
    message: str
    agent_id: str


class AgentStepPayload(BaseModel):
    """AGENT_STEP 事件的 payload"""
    phase: str


class AgentEndPayload(BaseModel):
    """AGENT_END 事件的 payload"""
    response_length: int
    final_text: str
    agent_id: str


class ErrorPayload(BaseModel):
    """ERROR 事件的 payload"""
    error: str
    phase: str


class LLMRequestPayload(BaseModel):
    """LLM_REQUEST 事件的 payload"""
    model: str
    timestamp: int


# ============= 3. 具体事件类型（带type字面量） =============

class MessageCreatedEvent(BaseEvent):
    """消息已创建事件"""
    type: Literal["message_created"] = "message_created"
    payload: MessageCreatedPayload


class JobCreatedEvent(BaseEvent):
    """任务已创建事件"""
    type: Literal["job_created"] = "job_created"
    payload: JobCreatedPayload


class JobStartedEvent(BaseEvent):
    """任务已开始事件"""
    type: Literal["job_started"] = "job_started"
    payload: JobStartedPayload = Field(default_factory=JobStartedPayload)


class JobCompletedEvent(BaseEvent):
    """任务已完成事件"""
    type: Literal["job_completed"] = "job_completed"
    payload: JobCompletedPayload = Field(default_factory=JobCompletedPayload)


class JobCancelledEvent(BaseEvent):
    """任务已取消事件"""
    type: Literal["job_cancelled"] = "job_cancelled"
    payload: JobCancelledPayload = Field(default_factory=JobCancelledPayload)


class JobFailedEvent(BaseEvent):
    """任务失败事件"""
    type: Literal["job_failed"] = "job_failed"
    payload: JobFailedPayload


class StatusChangeEvent(BaseEvent):
    """状态变更事件"""
    type: Literal["status_change"] = "status_change"
    payload: StatusChangePayload


class AgentStartEvent(BaseEvent):
    """Agent开始执行事件"""
    type: Literal["agent_start"] = "agent_start"
    payload: AgentStartPayload


class AgentStepEvent(BaseEvent):
    """Agent执行步骤事件"""
    type: Literal["agent_step"] = "agent_step"
    payload: AgentStepPayload


class AgentEndEvent(BaseEvent):
    """Agent执行结束事件"""
    type: Literal["agent_end"] = "agent_end"
    payload: AgentEndPayload


class ErrorEvent(BaseEvent):
    """错误事件"""
    type: Literal["error"] = "error"
    payload: ErrorPayload


class LLMRequestEvent(BaseEvent):
    """LLM请求事件"""
    type: Literal["llm_request"] = "llm_request"
    payload: LLMRequestPayload


# ============= 预留事件（当前未广泛使用，为兼容性保留） =============

class LogEvent(BaseEvent):
    """日志事件（预留）
    TODO: 需定义结构化 payload  schema（level、message、timestamp、source 等）
    """
    type: Literal["log"] = "log"
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolCallEvent(BaseEvent):
    """工具调用事件（预留）
    TODO: 需定义 ToolCallPayload（tool_id、parameters、result、duration 等字段）
    """
    type: Literal["tool_call"] = "tool_call"
    payload: dict[str, Any] = Field(default_factory=dict)


class FileWriteEvent(BaseEvent):
    """文件写入事件（预留）
    TODO: 需定义 FileWritePayload（path、operation、size、checksum 等字段）
    """
    type: Literal["file_write"] = "file_write"
    payload: dict[str, Any] = Field(default_factory=dict)


class ModelCallEvent(BaseEvent):
    """模型调用事件（已废弃，使用 LLM_REQUEST）
    TODO: 已废弃，未来版本移除，请迁移到 LLMRequestEvent
    """
    type: Literal["model_call"] = "model_call"
    payload: dict[str, Any] = Field(default_factory=dict)


# ============= 4. Discriminated Union 类型 =============

"""
事件联合类型，Pydantic 会根据 `type` 字段自动推断对应的 payload 类型。

使用示例：
    event: Event = Event.parse_obj(raw_dict)
    if event.type == "agent_start":
        # event 自动推断为 AgentStartEvent
        print(event.payload.message)  # 类型安全！
"""

Event = Union[
    MessageCreatedEvent,
    JobCreatedEvent,
    JobStartedEvent,
    JobCompletedEvent,
    JobCancelledEvent,
    JobFailedEvent,
    StatusChangeEvent,
    AgentStartEvent,
    AgentStepEvent,
    AgentEndEvent,
    ErrorEvent,
    LLMRequestEvent,
    # 预留事件（保持兼容）
    LogEvent,
    ToolCallEvent,
    FileWriteEvent,
    ModelCallEvent,
]
