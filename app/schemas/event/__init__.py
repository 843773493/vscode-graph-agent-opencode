"""
事件模式的 discriminated union 实现。

每个事件类型都有独立的、带 `type` 字面量字段的 Schema，
使用 Pydantic 的 discriminated union 功能实现类型安全。

参考：kilocode/packages/opencode/src/session/status.ts
"""

from datetime import datetime
from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, Field

# ============= 1. 基础事件结构（所有事件的公共字段） =============

class BaseEvent(BaseModel):
    """所有事件的基类"""
    event_id: str
    part_id: Optional[str] = None
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
    session_id: Optional[str] = None
    title: Optional[str] = None
    blocked_by_job_id: Optional[str] = None


class AgentStartPayload(BaseModel):
    """AGENT_START 事件的 payload"""
    message: str | None = None
    agent_id: str


class AgentStepPayload(BaseModel):
    """AGENT_STEP 事件的 payload"""
    phase: str | None = None


class ModelTokenUsagePayload(BaseModel):
    """一次用户回复内所有模型调用的累计 token 统计。"""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    model_calls: int = Field(default=0, ge=0)
    reported_model_calls: int = Field(default=0, ge=0)


class AgentEndPayload(BaseModel):
    """AGENT_END 事件的 payload"""
    final_text: str = ""
    agent_id: str
    token_usage: ModelTokenUsagePayload = Field(
        default_factory=ModelTokenUsagePayload
    )


class ToolCallStartPayload(BaseModel):
    """TOOL_CALL_START 事件的 payload"""
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    agent_id: str | None = None
    skill_names: list[str] = Field(default_factory=list)
    invocation_tool_name: str | None = None


class FileEditPayload(BaseModel):
    """工具产生的可审查文件变更摘要。"""
    edit_id: str
    file_path: str
    kind: str
    additions: int = 0
    deletions: int = 0
    diff_file: str
    before_file: str | None = None
    after_file: str | None = None


class ToolOutputReferencePayload(BaseModel):
    """过大工具输出在工作区内的持久化引用。"""
    type: Literal["tool_output"] = "tool_output"
    path: str
    read_path: str
    tool_name: str
    tool_call_id: str
    byte_count: int
    line_count: int
    content_sha256: str
    truncated: bool = True


class ToolCallEndPayload(BaseModel):
    """TOOL_CALL_END 事件的 payload"""
    tool_name: str
    result: str = ""
    agent_id: str | None = None
    invocation_tool_name: str | None = None
    skill_names: list[str] = Field(default_factory=list)
    tool_output: ToolOutputReferencePayload | None = None
    file_edit: FileEditPayload | None = None
    file_edits: list[FileEditPayload] = Field(default_factory=list)


class ErrorPayload(BaseModel):
    """ERROR 事件的 payload"""
    error: str
    phase: str


class LLMRequestPayload(BaseModel):
    """LLM_REQUEST 事件的 payload"""
    model: str
    timestamp: int


class SessionInterruptedPayload(BaseModel):
    """SESSION_INTERRUPTED 事件的 payload"""
    session_id: str
    phase: str
    tool_name: Optional[str] = None
    interrupted_at: datetime = Field(default_factory=datetime.now)


class TextDeltaPayload(BaseModel):
    """TEXT_DELTA 事件的 payload"""
    text: str
    kind: Literal["markdown", "reasoning"]


class TextStartPayload(BaseModel):
    """TEXT_START 事件的 payload（标记 assistant 文本开始）"""
    kind: Literal["markdown", "reasoning"]


class TextEndPayload(BaseModel):
    """TEXT_END 事件的 payload（标记 assistant 文本结束）"""
    kind: Literal["markdown", "reasoning"]
    text: str = ""


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


class ToolCallStartEvent(BaseEvent):
    """工具调用开始事件"""
    type: Literal["tool_call_start"] = "tool_call_start"
    payload: ToolCallStartPayload


class ToolCallEndEvent(BaseEvent):
    """工具调用结束事件"""
    type: Literal["tool_call_end"] = "tool_call_end"
    payload: ToolCallEndPayload


class ErrorEvent(BaseEvent):
    """错误事件"""
    type: Literal["error"] = "error"
    payload: ErrorPayload


class LLMRequestEvent(BaseEvent):
    """LLM请求事件"""
    type: Literal["llm_request"] = "llm_request"
    payload: LLMRequestPayload


class SessionInterruptedEvent(BaseEvent):
    """session 被用户打断事件"""
    type: Literal["session_interrupted"] = "session_interrupted"
    payload: SessionInterruptedPayload


class TextStartEvent(BaseEvent):
    """Assistant 文本开始事件"""
    type: Literal["text_start"] = "text_start"
    payload: TextStartPayload


class TextDeltaEvent(BaseEvent):
    """Assistant 文本增量事件"""
    type: Literal["text_delta"] = "text_delta"
    payload: TextDeltaPayload


class TextEndEvent(BaseEvent):
    """Assistant 文本结束事件"""
    type: Literal["text_end"] = "text_end"
    payload: TextEndPayload


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
    ToolCallStartEvent,
    ToolCallEndEvent,
    ErrorEvent,
    LLMRequestEvent,
    SessionInterruptedEvent,
    TextStartEvent,
    TextDeltaEvent,
    TextEndEvent,
] 
