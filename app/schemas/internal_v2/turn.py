from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import JobStatus
from .session import SessionDTO
from .trace import TraceEventDTO

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
# 上限直接从闭集派生，避免闭集与计数两份清单漂移。
MAX_TURN_INCLUDE_FIELDS = len(get_args(TurnInclude))
TurnCursorDirection = Literal["head", "tail", "before", "after", "around", "older"]
TurnCursorQueryMode = Literal["tail", "linear", "window", "execution"]
TurnProjectionState = Literal["ready", "partial"]


class TurnAttachmentDTO(BaseModel):
    """Turn 展示层只保存持久化附件引用，不携带内联 data URL。"""

    file_id: str
    name: str | None = None
    content_type: str | None = None
    availability: Literal["available", "unavailable"] | None = None


class TurnUserMessageDTO(BaseModel):
    """一次执行 Turn 所消费的用户可见消息。"""

    message_id: str
    content: str
    content_truncated: bool = False
    attachments: list[TurnAttachmentDTO] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(description="canonical 用户消息的不可变创建时刻，接口为带时区 ISO8601；沿消息 response_metadata.created_at，保留显式时区偏移，无时区时按 UTC 解释，缺失时报错。复制保留源消息时间事实。")


class TurnUserMessageSummaryDTO(BaseModel):
    message_id: str
    preview: str = Field(default="", max_length=500)
    content_truncated: bool = False
    attachment_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(description="对应用户消息详情的 created_at，接口为带时区 ISO8601；摘要直接复用详情时间，保留显式时区偏移，详情对无时区值按 UTC 解释，复制保留源消息时间事实。")


class TurnToolSummaryDTO(BaseModel):
    tool_name: str
    status: str
    tool_call_id: str | None = Field(default=None, description="历史工具聚合引用：已知 emitting ModelCall 时由其 ID 与 Provider 原始 call ID 限定，未知时保留原 call ID；不同 ModelCall 重用同一原始 ID 仍分别展示。")


class TurnThinkingBlockDTO(BaseModel):
    """安全的思考投影；encrypted 块只表达存在性，不携带 provider 密文。"""

    kind: Literal["reasoning", "summary", "encrypted"]
    text: str = Field(default="", max_length=4096)


class TurnResponseSourceDTO(BaseModel):
    """响应部件在 canonical rollout 中的稳定来源坐标。

    ``assistant_message_sequence`` 只用于把 ToolMessage 结果关联回产生它的
    assistant；它不是新的全局 part 序号。``call_index`` 仍然只表示同一
    assistant 的 tool_calls 列表顺序。

    历史 activity 的 ``created_at`` 沿原 item 创建时刻；``elapsed_ms`` 为逻辑
    activity 列表相邻项的整数毫秒差，首项从 Turn projection.created_at 起算。
    final 使用 final_item_created_at，elapsed_ms 固定为 0。standalone internal
    则沿同 view、同 execution 的前一 visible/display_only 逻辑成员，首项从
    execution.created_at 起算。负差钳为 0；streaming 可缺省这两个时间字段。
    """

    message_sequence: int = Field(ge=0)
    assistant_message_sequence: int | None = Field(default=None, ge=1)
    content_block_index: int | None = Field(default=None, ge=0)
    item_index: int | None = Field(default=None, ge=0)
    call_index: int | None = Field(default=None, ge=0)
    result_message_sequence: int | None = Field(default=None, ge=1)
    item_id: str | None = Field(default=None, min_length=1, max_length=256)
    item_sequence: int | None = Field(default=None, ge=1)
    logical_item_ordinal: int | None = Field(default=None, ge=0)
    part_ordinal: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)


class TurnResponsePartDTO(BaseModel):
    """历史和 live 共用的响应部件语义模型。"""

    part_id: str = Field(min_length=1, max_length=256, description="历史部件由 canonical item_id 与 part_ordinal 生成唯一引用；streaming 为临时部件 ID，不代表 canonical item 身份。")
    kind: Literal[
        "text",
        "reasoning",
        "reasoning_summary",
        "reasoning_encrypted",
        "compaction_summary",
        "tool_call",
        "tool_result",
        "final_text",
    ]
    projection: Literal["summary", "detail", "streaming"]
    status: Literal["pending", "running", "completed", "failed", "cancelled"] = Field(default="completed", description="响应部件的归一化状态；历史值由 canonical/消息投影映射，streaming 值随部件更新。completed 仅表示该部件完成，可与工具拒绝、Job 或 Execution 失败并存，不证明工具执行成功。")
    outcome_unknown: bool = Field(default=False, description="历史 terminal Turn 的本次 mapper 输入缺少同一 ModelCall 与 Provider call ID 的匹配结果；不证明底层工具未执行或执行失败。")
    source: TurnResponseSourceDTO
    text: str = Field(default="", max_length=65536)
    carrier_type: str | None = Field(default=None, max_length=64)
    tool_call_id: str | None = Field(default=None, max_length=256, description="保留 canonical payload 中合法的工具调用 ID 表示，可为 Provider 原始 ID 或已有 ModelCall 限定 ID；不等同于历史工具 Trace/摘要的聚合引用，也不等同于 part_id。")
    tool_name: str | None = Field(default=None, max_length=256)
    arguments: str | None = Field(default=None, max_length=65536)
    result: str | None = Field(default=None, max_length=65536)
    truncated: bool = False
    final: bool = False
    completion_reason: str | None = Field(default=None, max_length=64)
    partial: bool = False

    @model_validator(mode="after")
    def validate_source_identity(self) -> TurnResponsePartDTO:
        """历史 part 必须携带 canonical identity；live 只使用临时 part_id。"""
        if self.projection == "streaming":
            if self.source.item_id is not None or self.source.item_sequence is not None:
                raise ValueError("live streaming part 不得伪造 canonical item identity")
            return self
        if self.source.item_id is None or self.source.item_sequence is None:
            raise ValueError("历史 response part 缺少 canonical item identity")
        if self.source.created_at is None:
            raise ValueError("历史 response part 缺少 canonical created_at")
        return self


class TurnActivityStatsDTO(BaseModel):
    """Turn 折叠行使用的可展开中间 item 统计，不包含 Item 正文。

    历史 duration_ms 取 Turn projection.created_at 到 updated_at 的整数毫秒差。
    起止字符串缺失或非法时为 None，无时区按 UTC，负差钳为 0；这些是历史
    投影起止时刻，不能统一解释为 acceptance 到 terminal，也不同于 Web live 计时。
    """

    duration_ms: int | None = Field(default=None, ge=0)
    item_count: int = Field(default=0, ge=0)
    first_item_sequence: int | None = Field(default=None, ge=1, description="所属 rollout 中已选 activity 成员的物理 item_sequence 最小值；空 activity 为 null，不表示连续成员区间或跨副本稳定身份。")
    last_item_sequence: int | None = Field(default=None, ge=1, description="所属 rollout 中已选 activity 成员的物理 item_sequence 最大值；空 activity 为 null，不表示连续成员区间或跨副本稳定身份。")

    @model_validator(mode="after")
    def validate_item_range(self) -> TurnActivityStatsDTO:
        if self.item_count == 0:
            if self.first_item_sequence is not None or self.last_item_sequence is not None:
                raise ValueError("空活动统计不得携带 item sequence 范围")
            return self
        if self.first_item_sequence is None or self.last_item_sequence is None:
            raise ValueError("非空活动统计必须携带 item sequence 范围")
        if self.last_item_sequence < self.first_item_sequence:
            raise ValueError("活动统计 item sequence 范围非法")
        return self


class TurnBaseDTO(BaseModel):
    """Turn summary 与 detail 共享的稳定身份和状态。

    TurnRecord 与 acceptance 的持久 created_at 来自接受事务，root 消息有
    独立创建时刻；此处按 history 已加载消息投影，不把两种事件时间混同。
    """

    turn_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    root_logical_item_ordinal: int = Field(ge=0)
    # revision 精确绑定 Session/thread rollout 的 database_meta.history_view_revision；不是单 view、Turn 或 projection_epoch。
    # 同一 owner 的 COW 换 view 仍可比较，跨 owner 不可比；逐行冻结绑定，接受新页不会改写旧行。
    source_view_id: str = Field(min_length=1, description="实际历史读取快照的 view；消息操作原样携带。")
    source_view_revision: int = Field(ge=0, description="实际历史读取快照的 history_view_revision。")
    revision: int = Field(ge=1)
    status: JobStatus = Field(
        description="Turn 历史展示状态映射：open→accepted、active→running、completed_empty→completed、interrupted→timed_out、unknown→failed；这是 Turn 历史投影，不代表 Job 状态事实。",
    )
    created_at: datetime = Field(description="带时区 ISO8601 展示时刻：非空 Turn projection 的 created_at 会先校验为有效且带时区，即使消息自身有时间也不能省略。随后取本次实际加载的首个非 internal HumanMessage 的 response_metadata.created_at，否则取首个已加载消息；候选消息或其时间缺失时，回退已校验的 turns.created_at，两者均无可用时间则报错。非空但非法的消息日期字符串直接报错，不触发回退。消息时间保留显式时区偏移，无时区按 UTC 解释；展示值受 include 加载范围影响。")
    updated_at: datetime = Field(description="带时区 ISO8601 展示时刻：取本次所选 canonical Turn projection 的 turns.updated_at；缺少 projection、时刻非法或无时区时历史读取失败，不回退到消息时间；保留显式时区偏移。fork 历史沿用来源投影的原始时刻。")
    completed_at: datetime | None = Field(default=None, description="直接映射 updated_at 的同一时刻，不是独立完成事件，非终态 Turn 也可能有值；fork 历史沿用来源投影的原始时刻。")

    @model_validator(mode="after")
    def validate_turn_identity(self) -> TurnBaseDTO:
        if not self.turn_id.strip() or not self.job_id.strip():
            raise ValueError("Turn 与实际执行 Job ID 不能为空")
        if self.turn_id == self.job_id:
            raise ValueError("Turn ID 与实际执行 Job ID 必须独立")
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
    # 思考块按消息事件逐项保留；详情投影本身负责文本预算，不能在 DTO
    # 校验阶段因长回合超过 32 项而把整个 history 请求变成 400。
    thinking_blocks: list[TurnThinkingBlockDTO] = Field(default_factory=list)
    tool_summary: list[TurnToolSummaryDTO] = Field(default_factory=list, max_length=64)
    tool_summary_truncated: bool = False
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
    # 思考块按消息事件逐项保留；详情投影本身负责文本预算，不能在 DTO
    # 校验阶段因长回合超过 32 项而把整个 history 请求变成 400。
    thinking_blocks: list[TurnThinkingBlockDTO] = Field(default_factory=list)
    tool_summary: list[TurnToolSummaryDTO] = Field(default_factory=list, max_length=64)
    tool_summary_truncated: bool = False
    response_parts: list[TurnResponsePartDTO] = Field(
        default_factory=list,
        max_length=512,
    )
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
    tool_call_ids: list[str] | None = Field(default=None, min_length=1, max_length=4)
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
    def validate_tool_call_selector(self) -> TurnDetailBatchRequest:
        if self.tool_call_ids is None:
            return self
        if len(self.turn_ids) != 1:
            raise ValueError("按 tool_call_ids 加载详情一次只能指定一个 turn_id")
        if len(set(self.tool_call_ids)) != len(self.tool_call_ids):
            raise ValueError("tool_call_ids 不能重复")
        if self.include is not None and not set(self.include) & {"tool_call", "tool_result"}:
            raise ValueError("按 tool_call_ids 加载详情必须请求 tool_call 或 tool_result")
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
    execution_id: str | None = Field(default=None, min_length=1, max_length=256)
    # 只用于单个 Turn 内的 ToolRow 定点补载；不允许退化成整轮工具详情读取。
    tool_call_ids: list[str] | None = Field(default=None, min_length=1, max_length=4)
    turns: int | None = Field(default=None, ge=1, le=256)
    before_turns: int | None = Field(default=None, ge=0, le=256)
    after_turns: int | None = Field(default=None, ge=0, le=256)
    include: list[TurnInclude] | None = Field(
        default=None,
        max_length=MAX_TURN_INCLUDE_FIELDS,
    )

    @model_validator(mode="after")
    def validate_around_budget(self) -> TurnHistoryLoadRequest:
        if self.execution_id is not None:
            if any(value is not None for value in (
                self.turn_ids, self.tool_call_ids, self.anchor_turn_id,
                self.turns, self.before_turns, self.after_turns,
            )):
                raise ValueError("execution_id 详情不能混用 Turn 选择器或窗口")
            if self.direction not in {"tail", "before"}:
                raise ValueError("execution_id 详情只支持 tail 与 before 续读")
            if self.include is not None and "internal" not in self.include:
                raise ValueError("execution_id 详情必须包含 internal")
            return self
        if self.turn_ids is not None:
            if len(set(self.turn_ids)) != len(self.turn_ids):
                raise ValueError("turn_ids 不能重复")
            if self.cursor is not None and len(self.turn_ids) != 1:
                raise ValueError("详情续读一次只能指定一个 turn_id")
            if self.anchor_turn_id is not None:
                raise ValueError("按 turn_ids 加载详情不能提供 anchor_turn_id")
            if self.before_turns is not None or self.after_turns is not None:
                raise ValueError("按 turn_ids 加载详情不能提供两侧 Turn 数量")
            if self.tool_call_ids is not None:
                if len(self.turn_ids) != 1:
                    raise ValueError("按 tool_call_ids 加载详情一次只能指定一个 turn_id")
                if len(set(self.tool_call_ids)) != len(self.tool_call_ids):
                    raise ValueError("tool_call_ids 不能重复")
                if self.include is not None and not set(self.include) & {
                    "tool_call",
                    "tool_result",
                }:
                    raise ValueError(
                        "按 tool_call_ids 加载详情必须请求 tool_call 或 tool_result"
                    )
            return self
        if self.tool_call_ids is not None:
            raise ValueError("tool_call_ids 必须和 turn_ids 一起提供")
        if self.direction == "around":
            if self.cursor is None and self.anchor_turn_id is None:
                raise ValueError("around 读取必须提供 cursor 或 anchor_turn_id")
        elif self.before_turns is not None or self.after_turns is not None:
            raise ValueError("只有 around 读取可以提供两侧 Turn 数量")
        elif self.anchor_turn_id is not None:
            raise ValueError("只有 around 读取可以提供 anchor_turn_id")
        return self


class InternalDisplayProvenanceDTO(BaseModel):
    """仅包含 canonical display part 已记录的 typed 来源 identity。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_session_id: str | None = Field(default=None, min_length=1, max_length=256)
    source_thread_id: str | None = Field(default=None, min_length=1, max_length=256)
    delegation_id: str | None = Field(default=None, min_length=1, max_length=256)
    source_execution_id: str | None = Field(default=None, min_length=1, max_length=256)
    source_item_id: str | None = Field(default=None, min_length=1, max_length=256)


class InternalExecutionDisplayPartDTO(BaseModel):
    """独立 internal execution 的安全展示文本和 canonical 定位。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["generated_session_result"]
    text: str = Field(min_length=1, max_length=4096)
    item_id: str = Field(min_length=1, max_length=256)
    item_sequence: int = Field(ge=1)
    logical_item_ordinal: int = Field(ge=0)
    created_at: datetime
    provenance: InternalDisplayProvenanceDTO

    @model_validator(mode="after")
    def validate_safe_text(self) -> InternalExecutionDisplayPartDTO:
        if not self.text.strip():
            raise ValueError("internal display text 不能为空")
        return self


class InternalExecutionHistoryDTO(BaseModel):
    """Turn 之外的 standalone internal execution 历史投影。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str = Field(min_length=1, max_length=256)
    job_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(
        min_length=1,
        max_length=256,
        description="该持久 execution/admission 的历史 Session scope；fork 保留原值，可不同于本页请求目标，仅由目标本地冻结 namespace binding 校验，不用于重新寻址来源。",
    )
    thread_id: str = Field(
        min_length=1,
        max_length=256,
        description="该持久 execution/admission 的历史 thread scope；fork 保留原值，可不同于本页请求 thread，仅由目标本地冻结 namespace binding 校验，不用于重新寻址来源。",
    )
    status: Literal[
        "completed",
        "completed_empty",
        "failed",
        "interrupted",
        "cancelled",
        "execution_lost",
        "unknown",
    ] = Field(description="对应 executions.outcome 的独立 internal execution 历史结果，不是 Job 或 Turn 状态。")
    created_at: datetime = Field(description="对应 executions 持久记录不可变的 UTC 创建时刻，接口按 ISO8601 表达，复制保留源值。")
    updated_at: datetime = Field(description="对应 executions.updated_at 的来源更新时刻，接口按 ISO8601 表达；fork 保留全部历史行的原始时间，不用目标安装、发布或取消时间覆盖。")
    display_parts: list[InternalExecutionDisplayPartDTO] = Field(
        max_length=256,
    )
    response_parts: list[TurnResponsePartDTO] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def validate_response_part_identity(self) -> InternalExecutionHistoryDTO:
        for part in self.response_parts:
            if part.source.part_ordinal is None:
                raise ValueError(
                    "internal execution response part 缺少 canonical part_ordinal"
                )
            if part.source.logical_item_ordinal is None:
                raise ValueError(
                    "internal execution response part 缺少 membership ordinal"
                )
        return self


class TurnHistoryPageDTO(BaseModel):
    items: list[TurnDetailDTO] = Field(max_length=256)
    summaries: list[TurnSummaryDTO] = Field(default_factory=list, max_length=256)
    internal_executions: list[InternalExecutionHistoryDTO] = Field(
        default_factory=list,
        max_length=256,
    )
    # 本页实际读取的请求 thread owner：业务层解析显式 thread 或权威 main pointer 后回填；独立 execution DTO 可保留不同的历史 owner。
    thread_id: str | None = None
    # revision 精确绑定 Session/thread rollout 的 database_meta.history_view_revision；不是单 view、Turn 或 projection_epoch。
    # 同一 owner 的 COW 换 view 仍可比较，跨 owner 不可比；本页绑定冻结，接受新页不会改写既有行。
    source_view_id: str | None = Field(default=None, min_length=1)
    source_view_revision: int | None = Field(default=None, ge=0)
    next_cursor: str | None = None
    has_more: bool = False
    before_cursor: str | None = None
    after_cursor: str | None = None
    has_before: bool = False
    has_after: bool = False
    projection_epoch: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_internal_execution_identity(self) -> TurnHistoryPageDTO:
        if (self.source_view_id is None) != (self.source_view_revision is None):
            raise ValueError("历史页必须同时携带 source view/revision")
        execution_ids = [entry.execution_id for entry in self.internal_executions]
        if len(execution_ids) != len(set(execution_ids)):
            raise ValueError("internal_executions 中 execution_id 不能重复")
        item_ids = [
            part.item_id
            for entry in self.internal_executions
            for part in entry.display_parts
        ]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("internal_executions 中 display item_id 不能重复")
        response_item_ids = [
            part.part_id
            for entry in self.internal_executions
            for part in entry.response_parts
        ]
        if len(response_item_ids) != len(set(response_item_ids)):
            raise ValueError("internal_executions 中 response part_id 不能重复")
        return self


class TurnJobSummaryDTO(BaseModel):
    job_id: str
    message_id: str
    status: JobStatus
    updated_at: datetime


class SessionTurnBootstrapDTO(BaseModel):
    session: SessionDTO
    # 历史/实时事件实际读取的 thread 身份；普通入口恒为权威 main thread。
    thread_id: str = Field(min_length=1)
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

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[2] = 2
    session_id: str
    thread_id: str
    checkpoint_ns: str
    rollout_id: str
    projection_epoch: int = Field(ge=1, strict=True)
    view_id: str = Field(min_length=1)
    history_view_revision: int = Field(ge=0, strict=True)
    turn_read_upper_bound: int | None = Field(default=None, ge=1, strict=True)
    logical_item_read_upper_bound: int | None = Field(default=None, ge=0, strict=True)
    direction: Literal["before", "after"]
    query_mode: TurnCursorQueryMode
    effective_include: tuple[TurnInclude, ...]
    anchor_turn_id: str | None = Field(default=None, min_length=1, max_length=256)
    window_before_turns: int | None = Field(default=None, ge=0, le=256, strict=True)
    window_after_turns: int | None = Field(default=None, ge=0, le=256, strict=True)
    execution_id: str | None = Field(default=None, min_length=1, max_length=256)
    stage: int = Field(default=0, ge=0, strict=True)
    turn_ordinal: int | None = Field(default=None, ge=1, strict=True)
    logical_item_ordinal: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_query_binding(self) -> TurnCursorDTO:
        if (self.query_mode == "execution") != (self.execution_id is not None):
            raise ValueError("execution 历史游标必须冻结精确 execution_id")
        if self.effective_include != tuple(sorted(set(self.effective_include))):
            raise ValueError("历史游标 effective_include 必须排序且唯一")
        window_values = (
            self.anchor_turn_id,
            self.window_before_turns,
            self.window_after_turns,
        )
        if self.query_mode == "window":
            if any(value is None for value in window_values):
                raise ValueError("window 历史游标必须冻结 anchor 与两侧窗口")
        elif any(value is not None for value in window_values):
            raise ValueError("非 window 历史游标不得携带 around 窗口")
        if (
            self.turn_ordinal is not None
            and self.turn_read_upper_bound is not None
            and self.turn_ordinal > self.turn_read_upper_bound
        ):
            raise ValueError("历史游标 turn anchor 超过 snapshot upper bound")
        if self.turn_read_upper_bound is None and self.turn_ordinal is not None:
            raise ValueError("空 Turn snapshot 不得携带 turn anchor")
        if (
            self.logical_item_ordinal is not None
            and self.logical_item_read_upper_bound is not None
            and self.logical_item_ordinal > self.logical_item_read_upper_bound
        ):
            raise ValueError("历史游标 item anchor 超过 snapshot upper bound")
        if (
            self.logical_item_read_upper_bound is None
            and self.logical_item_ordinal is not None
        ):
            raise ValueError("空 internal snapshot 不得携带 item anchor")
        if "internal" not in self.effective_include and self.logical_item_ordinal is not None:
            raise ValueError("effective_include 排除 internal 时不得携带 item anchor")
        if self.turn_ordinal is None and self.logical_item_ordinal is None:
            raise ValueError("已耗尽的历史 cursor 不得继续发行")
        return self


class StaleTurnCursorErrorDTO(BaseModel):
    code: Literal["stale_turn_cursor"] = "stale_turn_cursor"
    reason: Literal["projection_epoch", "history_view_revision"] = "projection_epoch"
    session_id: str
    cursor_epoch: int = Field(ge=1)
    current_epoch: int = Field(ge=1)
    cursor_history_view_revision: int | None = Field(default=None, ge=0)
    current_history_view_revision: int | None = Field(default=None, ge=0)
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
