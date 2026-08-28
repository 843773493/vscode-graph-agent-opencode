from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import JobStatus
from .session import SessionDTO
from .trace import TraceEventDTO

TurnItemsView = Literal["summary", "full"]
MAX_TURN_INCLUDE_FIELDS = 14
TurnInclude = Literal[
    "user",
    "text",
    "assistant_text",
    "assistant",
    "thinking",
    "reasoning_summary",
    "reasoning_detail",
    "encrypted_reasoning_meta",
    "tool_summary",
    "tool_call",
    "tool_result",
    "internal",
    "metadata",
    "final_response",
]
TurnCursorDirection = Literal["head", "tail", "before", "after", "around", "older"]
TurnProjectionState = Literal["ready", "partial"]


class TurnAttachmentDTO(BaseModel):
    """Turn 展示层只保存持久化附件引用，不携带内联 data URL。"""

    file_id: str
    name: str | None = None
    content_type: str | None = None


class TurnUserMessageDTO(BaseModel):
    """一次执行 Turn 所消费的用户可见消息。"""

    message_id: str
    content: str
    content_truncated: bool = False
    attachments: list[TurnAttachmentDTO] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class TurnUserMessageSummaryDTO(BaseModel):
    message_id: str
    preview: str = Field(default="", max_length=500)
    content_truncated: bool = False
    attachment_count: int = Field(default=0, ge=0)
    created_at: datetime


class TurnToolSummaryDTO(BaseModel):
    tool_name: str
    status: str
    tool_call_id: str | None = None


class TurnThinkingBlockDTO(BaseModel):
    """安全的思考投影；encrypted 块只表达存在性，不携带 provider 密文。"""

    kind: Literal["reasoning", "summary", "encrypted"]
    text: str = Field(default="", max_length=4096)


class TurnResponseSourceDTO(BaseModel):
    """响应部件在 canonical rollout 中的稳定来源坐标。

    ``assistant_message_sequence`` 只用于把 ToolMessage 结果关联回产生它的
    assistant；它不是新的全局 part 序号。``call_index`` 仍然只表示同一
    assistant 的 tool_calls 列表顺序。
    """

    message_sequence: int = Field(ge=1)
    assistant_message_sequence: int | None = Field(default=None, ge=1)
    content_block_index: int | None = Field(default=None, ge=0)
    item_index: int | None = Field(default=None, ge=0)
    call_index: int | None = Field(default=None, ge=0)
    result_message_sequence: int | None = Field(default=None, ge=1)


class TurnResponsePartDTO(BaseModel):
    """历史和 live 共用的响应部件语义模型。"""

    part_id: str = Field(min_length=1, max_length=256)
    kind: Literal[
        "text",
        "reasoning",
        "reasoning_summary",
        "reasoning_encrypted",
        "tool_call",
        "tool_result",
        "final_text",
    ]
    projection: Literal["summary", "detail", "streaming"]
    status: Literal["pending", "running", "completed", "failed", "cancelled"] = "completed"
    source: TurnResponseSourceDTO
    text: str = Field(default="", max_length=65536)
    carrier_type: str | None = Field(default=None, max_length=64)
    tool_call_id: str | None = Field(default=None, max_length=256)
    tool_name: str | None = Field(default=None, max_length=256)
    arguments: str | None = Field(default=None, max_length=65536)
    result: str | None = Field(default=None, max_length=65536)
    truncated: bool = False
    final: bool = False


class TurnActivityStatsDTO(BaseModel):
    """Turn 折叠行使用的轻量 rollout message 统计，不包含消息正文。"""

    duration_ms: int | None = Field(default=None, ge=0)
    message_count: int = Field(default=0, ge=0)


class TurnBaseDTO(BaseModel):
    """Turn summary 与 detail 共享的稳定身份和状态。"""

    turn_id: str
    job_id: str
    session_id: str
    ordinal: int = Field(ge=1)
    revision: int = Field(ge=1)
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_turn_identity(self) -> TurnBaseDTO:
        if self.turn_id != self.job_id:
            raise ValueError("Turn ID 必须等于实际执行 Job ID")
        return self


class TurnSummaryDTO(TurnBaseDTO):
    items_view: Literal["summary"] = "summary"
    source_message_ids: list[str] = Field(default_factory=list, max_length=32)
    source_message_count: int = Field(default=0, ge=0)
    merged_job_ids: list[str] = Field(default_factory=list, max_length=32)
    merged_job_count: int = Field(default=0, ge=0)
    sources_truncated: bool = False
    user_messages: list[TurnUserMessageSummaryDTO] = Field(
        default_factory=list,
        max_length=8,
    )
    user_message_count: int = Field(default=0, ge=0)
    user_messages_truncated: bool = False
    response_preview: str = Field(default="", max_length=1000)
    preview_truncated: bool = False
    item_count: int = Field(default=0, ge=0)
    thinking_blocks: list[TurnThinkingBlockDTO] = Field(default_factory=list, max_length=32)
    tool_summary: list[TurnToolSummaryDTO] = Field(default_factory=list, max_length=64)
    response_parts: list[TurnResponsePartDTO] = Field(default_factory=list, max_length=128)
    activity_stats: TurnActivityStatsDTO = Field(default_factory=TurnActivityStatsDTO)


class TurnDetailDTO(TurnBaseDTO):
    items_view: Literal["full"] = "full"
    source_message_ids: list[str] = Field(default_factory=list)
    merged_job_ids: list[str] = Field(default_factory=list)
    user_messages: list[TurnUserMessageDTO] = Field(default_factory=list)
    response_preview: str = Field(default="", max_length=1000)
    preview_truncated: bool = False
    assistant_text: list[str] = Field(default_factory=list, max_length=32)
    thinking_blocks: list[TurnThinkingBlockDTO] = Field(default_factory=list, max_length=32)
    tool_summary: list[TurnToolSummaryDTO] = Field(default_factory=list, max_length=64)
    response_parts: list[TurnResponsePartDTO] = Field(default_factory=list, max_length=512)
    final_response: str = ""
    items: list[TraceEventDTO] = Field(default_factory=list)
    detail_truncated: bool = False
    detail_next_cursor: str | None = None
    activity_stats: TurnActivityStatsDTO = Field(default_factory=TurnActivityStatsDTO)

    @model_validator(mode="after")
    def validate_source_identity(self) -> TurnDetailDTO:
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("Turn source_message_ids 不能重复")
        if len(set(self.merged_job_ids)) != len(self.merged_job_ids):
            raise ValueError("Turn merged_job_ids 不能重复")
        return self


class TurnPageDTO(BaseModel):
    """内部 Turn 投影仓储使用的 summary 页面。"""

    items: list[TurnSummaryDTO] = Field(max_length=20)
    next_cursor: str | None = None
    has_more: bool = False
    projection_epoch: int = Field(ge=1)


class TurnDetailBatchRequest(BaseModel):
    """内部 Turn 投影仓储使用的详情批次类型；HTTP 入口统一使用 history。"""

    model_config = ConfigDict(extra="forbid")

    turn_ids: list[str] = Field(min_length=1, max_length=4)
    include: list[TurnInclude] | None = Field(
        default=None,
        max_length=MAX_TURN_INCLUDE_FIELDS,
    )

    @model_validator(mode="after")
    def validate_unique_turn_ids(self) -> TurnDetailBatchRequest:
        if len(set(self.turn_ids)) != len(self.turn_ids):
            raise ValueError("turn_ids 不能重复")
        return self

    @model_validator(mode="after")
    def validate_unique_include(self) -> TurnDetailBatchRequest:
        if self.include is not None and len(set(self.include)) != len(self.include):
            raise ValueError("include 不能重复")
        return self


class TurnDetailBatchDTO(BaseModel):
    """内部 Turn 投影仓储使用的详情批次类型。"""

    items: list[TurnDetailDTO] = Field(max_length=4)
    projection_epoch: int = Field(ge=1)
    next_cursor: str | None = None
    has_more: bool = False


class TurnDetailCursorDTO(BaseModel):
    """详情 payload 的不透明续读游标。"""

    version: Literal[1] = 1
    session_id: str
    projection_epoch: int = Field(ge=1)
    turn_id: str
    event_index: int = Field(ge=0)
    include_hash: str = Field(min_length=1, max_length=64)


class TurnHistoryLoadRequest(BaseModel):
    """语义化历史读取请求；客户端不能通过它绕过服务端硬上限。"""

    model_config = ConfigDict(extra="forbid")

    direction: TurnCursorDirection = "tail"
    cursor: str | None = None
    anchor_turn_id: str | None = Field(default=None, min_length=1, max_length=256)
    turn_ids: list[str] | None = Field(default=None, min_length=1, max_length=4)
    turns: int | None = Field(default=None, ge=1, le=256)
    before_turns: int | None = Field(default=None, ge=0, le=256)
    after_turns: int | None = Field(default=None, ge=0, le=256)
    include: list[TurnInclude] | None = Field(
        default=None,
        max_length=MAX_TURN_INCLUDE_FIELDS,
    )

    @model_validator(mode="after")
    def validate_around_budget(self) -> TurnHistoryLoadRequest:
        if self.turn_ids is not None:
            if len(set(self.turn_ids)) != len(self.turn_ids):
                raise ValueError("turn_ids 不能重复")
            if self.cursor is not None and len(self.turn_ids) != 1:
                raise ValueError("详情续读一次只能指定一个 turn_id")
            if self.anchor_turn_id is not None:
                raise ValueError("按 turn_ids 加载详情不能提供 anchor_turn_id")
            if self.before_turns is not None or self.after_turns is not None:
                raise ValueError("按 turn_ids 加载详情不能提供两侧 Turn 数量")
            return self
        if self.direction == "around":
            if self.cursor is None and self.anchor_turn_id is None:
                raise ValueError("around 读取必须提供 cursor 或 anchor_turn_id")
        elif self.before_turns is not None or self.after_turns is not None:
            raise ValueError("只有 around 读取可以提供两侧 Turn 数量")
        elif self.anchor_turn_id is not None:
            raise ValueError("只有 around 读取可以提供 anchor_turn_id")
        return self


class TurnHistoryPageDTO(BaseModel):
    items: list[TurnDetailDTO] = Field(max_length=256)
    next_cursor: str | None = None
    has_more: bool = False
    before_cursor: str | None = None
    after_cursor: str | None = None
    has_before: bool = False
    has_after: bool = False
    projection_epoch: int = Field(ge=1)


class TurnJobSummaryDTO(BaseModel):
    job_id: str
    message_id: str
    status: JobStatus
    updated_at: datetime


class SessionTurnBootstrapDTO(BaseModel):
    session: SessionDTO
    latest_turn: TurnSummaryDTO | None = None
    active_job_id: str | None = None
    active_jobs: list[TurnJobSummaryDTO] = Field(default_factory=list, max_length=8)
    active_job_count: int = Field(default=0, ge=0)
    active_jobs_truncated: bool = False
    projection_state: TurnProjectionState = "ready"
    older_cursor: str | None = None
    event_cursor: str | None = None
    projection_epoch: int = Field(ge=1)


class TurnCursorDTO(BaseModel):
    """服务端编码进不透明 cursor 的稳定锚点。"""

    version: Literal[1] = 1
    session_id: str
    projection_epoch: int = Field(ge=1)
    anchor_turn_id: str
    include_anchor: bool = False
    direction: TurnCursorDirection = "older"
    stage: int = Field(default=0, ge=0)


class StaleTurnCursorErrorDTO(BaseModel):
    code: Literal["stale_turn_cursor"] = "stale_turn_cursor"
    session_id: str
    cursor_epoch: int = Field(ge=1)
    current_epoch: int = Field(ge=1)
    message: str


class StaleTurnReferenceErrorDTO(BaseModel):
    """请求的 Turn 仍存在于 rollout，但不再属于当前 context view。"""

    code: Literal["stale_turn_reference"] = "stale_turn_reference"
    session_id: str
    turn_ids: list[str] = Field(min_length=1, max_length=4)
    message: str


class TurnProjectionCorruptedErrorDTO(BaseModel):
    code: Literal["turn_projection_corrupted"] = "turn_projection_corrupted"
    session_id: str
    message: str
