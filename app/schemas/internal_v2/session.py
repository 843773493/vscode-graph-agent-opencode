from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.session_catalog_store.contracts import (
    SessionGenerationOriginDTO,
    SessionKind,
)
from app.schemas.canonical_params import CanonicalThreadId

from .common import TimestampedDTO
from .session_context import SessionContextScope, SessionResourceIdentity
from .session_resource import SessionResourceKind
from .team import TeamBoardDTO
from .workspace_resource import WorkspaceResourceDTO

TitleSource = Literal["default", "user", "auto"]
SessionForkMode = Literal["context_fork", "view_prefix_fork", "full_rollout_copy"]
ChildThreadStatus = Literal["idle", "pending", "running", "completed", "failed"]
SessionForkCreationKind = Literal["new_session_main", "new_child_thread"]
SessionForkResourceAction = Literal["reuse_reference", "retain_reference", "copy_snapshot", "omit", "unsupported"]


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = "新会话"
    agent_id: str | None = None
    folder_id: str | None = None


class SessionTaskContext(BaseModel):
    """委派 child 的历史初始化选择；来源固定为实际调用 task 的 Thread。"""

    # TODO(task-prefix-selector): 模型输出调用会推进历史 revision；冻结前缀选择凭证落地后再开放显式前缀。

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["empty", "context_fork"] = "empty"


class SessionForkOwnerReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["terminal", "browser"]
    resource_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=1, description="attach 的 CAS 预期版本，必须为正数并与当前 owner revision 匹配。")


class SessionForkResourceChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_uri: str | None = Field(default=None, min_length=1, exclude_if=lambda value: value is None)
    action: SessionForkResourceAction
    owner_reference: SessionForkOwnerReference | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if (self.resource_uri is None) == (self.owner_reference is None):
            raise ValueError("fork resource 必须且只能提供静态 URI 或 typed owner reference")
        if self.owner_reference is not None and self.action not in {"reuse_reference", "omit"}:
            raise ValueError("live resource 只能显式复用原实例或不继承")
        return self


class SessionForkGoalSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_budget: int | None = Field(default=None, gt=0)


class SessionForkResourceResult(BaseModel):
    resource_uri: str | None = Field(default=None, exclude_if=lambda value: value is None)
    action: SessionForkResourceAction
    availability: Literal["available", "unavailable"]
    target_uri: str | None = None
    reason: str | None = None
    owner_reference: SessionForkOwnerReference | None = Field(default=None, exclude_if=lambda value: value is None)
    owner_resource: WorkspaceResourceDTO | None = Field(default=None, exclude_if=lambda value: value is None)
    operation_state: Literal["not_dispatched", "dispatching", "committed", "rejected"] | None = Field(default=None, exclude_if=lambda value: value is None)
    committed_revision: int | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="本次 attach 的历史提交 revision；尚未提交时省略，当前 owner 版本见 owner_resource.owner_revision。",
    )


class SessionForkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1)
    source_uri: str = Field(min_length=1, description="精确来源 Thread URI；Session 兼容入口由 service 按 catalog main 归一。")
    source_view_id: str = Field(min_length=1, description="来源 Thread 中所选历史 view 的冻结身份。")
    source_view_revision: int = Field(ge=0, description="冻结所选来源 view 时的历史 revision；提交后不得追随 latest。")
    anchor_item_id: str | None = Field(default=None, min_length=1, description="冻结来源 view 中点击的稳定 item 身份。")
    mode: SessionForkMode = "view_prefix_fork"
    anchor_mode: Literal["inclusive", "before"] = Field(default="inclusive", description="inclusive 包含点击 item；before 不包含点击 item。")
    target_container_uri: str = Field(min_length=1, description="创建容器 URI；结果 thread_uri/thread_id 才是实际新目标 Thread。")
    creation_kind: SessionForkCreationKind = "new_child_thread"
    title: str | None = None
    relationship: Literal["detached", "pinned"] = "detached"
    resources: list[SessionForkResourceChoice] = Field(default_factory=list)
    goal: SessionForkGoalSelection | None = None

    @model_validator(mode="after")
    def validate_fork_point(self) -> Self:
        if self.mode == "view_prefix_fork" and self.anchor_item_id is None:
            raise ValueError("view_prefix_fork 必须提供点击 item")
        if self.mode == "full_rollout_copy" and (self.anchor_item_id is not None or self.anchor_mode != "inclusive"):
            raise ValueError("full_rollout_copy 不能携带截断锚点")
        if self.creation_kind == "new_child_thread" and self.goal is not None:
            raise ValueError("Goal 只能在显式创建新 Session main 时继承")
        identities = [
            ("uri", choice.resource_uri) if choice.owner_reference is None else
            (choice.owner_reference.kind, choice.owner_reference.workspace_id, choice.owner_reference.resource_id)
            for choice in self.resources
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("同一 fork resource 不能重复选择")
        return self


class SessionForkResourceSourceDTO(BaseModel):
    """冻结正文依赖及可选静态资源的来源事实；不表示目标许可。"""

    resource_uri: str
    name: str | None = Field(description="领域 owner 的资源名称展示投影，不由 URI 推断。")
    content_type: str | None = Field(description="领域 owner 的资源正文类型展示投影。")
    availability: Literal["available", "unavailable"] = Field(description="来源 owner/blob 的可用性事实，不表示目标授权或可复制许可。")
    required_for_context: bool = Field(description="本次冻结选择的正文依赖，不表示附件的永久属性或目标授权。")


class SessionForkSourcePointDTO(BaseModel):
    """服务按真实 Thread 索引冻结的弹窗来源；前端不猜 item 或追随 latest。"""

    source_uri: str
    source_view_id: str
    source_view_revision: int
    anchor_item_id: str | None = Field(description="所选范围的有效截止 item；完整历史或无默认点击锚点时为空。")
    child_container_uri: str
    main_container_uri: str
    title: str = Field(description="新目标名称的可编辑预填值，不是来源 Session 名称。")
    goal_available: bool = Field(description="来源 Session 是否有可选 Goal，不表示已经继承到目标。")
    resources: list[SessionForkResourceSourceDTO]
    resource_mode: SessionForkMode
    resource_anchor_mode: Literal["inclusive", "before"]
    live_resources: list[WorkspaceResourceDTO] = Field(default_factory=list)


class SessionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = None
    provider_id: str | None = None


class SessionDTO(TimestampedDTO):
    """Session catalog 投影；导航祖先、fork 来源及会话时间各有独立来源。"""

    created_at: datetime = Field(description="Session 创建 intent 冻结并由 catalog 投影的 UTC 创建时刻；导航改名和移动不重写。")
    updated_at: datetime = Field(description="catalog 记录的 Session 配置最近一次更新 UTC 时刻；初建等于 created_at，导航改名和移动不推进。")
    session_id: str
    workspace_id: str
    resource_identity: SessionResourceIdentity
    uri: str
    scope: SessionContextScope
    title: str
    title_source: TitleSource = "default"
    current_agent_id: str
    current_provider_id: str | None = None
    parent_session_id: str | None = Field(default=None, description="catalog 导航父链中最近祖先 Session 的稳定 ID；随导航移动派生，不表示物理父目录。")
    context_source_session_id: str | None = Field(default=None, description="创建 metadata 冻结的 fork 来源 Session 稳定 ID；独立于导航祖先，普通 Session 可为空。")
    kind: SessionKind = "normal"
    generation_origin: SessionGenerationOriginDTO | None = None
    # 普通 Session 入口显式解析出的 main thread 身份；child thread 只能经
    # 精确 (session_id, thread_id) 定位，这里不回退成 session_id。
    thread_id: str | None = None

    @model_validator(mode="after")
    def validate_internal_origin(self) -> Self:
        if self.kind == "context_fork" and self.context_source_session_id is None:
            raise ValueError("context_fork 会话缺少 context_source_session_id")
        return self


class SessionListResultDTO(BaseModel):
    items: list[SessionDTO]
    total: int
    next_cursor: str | None = None
    has_more: bool = False


class SessionForkExportReleaseDTO(BaseModel):
    state: Literal["pending", "confirmed"]
    error: str | None = None


class SessionForkResultDTO(BaseModel):
    session: SessionDTO = Field(description="实际目标 Thread 所属的完整目标 Session；不表示应切换主会话。")
    thread_id: str = Field(description="实际新目标 Thread 的稳定身份。")
    thread_uri: str = Field(description="实际新目标 Thread URI，区别于请求的创建容器 URI。")
    target_gateway_id: str = Field(min_length=1, description="实际发布目标的稳定 Gateway 身份；客户端据此匹配已登记路由，不自行解析 URI。")
    creation_kind: SessionForkCreationKind
    source_uri: str
    source_view_id: str
    source_view_revision: int
    anchor_item_id: str | None = None
    mode: SessionForkMode
    resources: list[SessionForkResourceResult] = Field(default_factory=list)
    goal_id: str | None = None
    export_release: SessionForkExportReleaseDTO | None = None


class ChildThreadBlockerDTO(BaseModel):
    kind: str
    reason: str


class ChildThreadRuntimeDTO(BaseModel):
    residency: Literal["cold", "loading", "resident", "unloading"]
    cold_eligible: bool
    execution_state: str
    last_activity_at: datetime | None
    idle_deadline: datetime | None
    blockers: list[ChildThreadBlockerDTO]


class ChildThreadSummaryDTO(BaseModel):
    """owner Session 内 durable child thread 的只读投影。

    数据来自 owner session-control.sqlite 的 thread_catalog（child
    row）+ collaboration ledger/member + initial execution intent；
    status 是组合 admission、协作成员、fork 和报告 outcome 的展示状态，
    不是直接的执行运行状态；admission_state 与 collaboration_state
    继续保留为诊断字段。
    """

    thread_id: str
    created_at: datetime
    delegation_id: str | None = None
    role: str | None = None
    subagent_type: str | None = None
    title: str | None = None
    collaboration_state: str | None = None
    admission_state: str | None = None
    status: ChildThreadStatus = Field(
        description="子线程合成展示状态：初始 intent pending→pending、bound→running；已取消协作成员→failed；已发布 fork→idle；报告 execution 有终态 outcome 时 completed/completed_empty→completed，其它已知失败终态→failed。bound 只表示 admission 已绑定，不保证 Job 已开始。",
    )
    runtime: ChildThreadRuntimeDTO | None = None
    report_execution_id: str | None = None
    report_status: str | None = None


class ChildThreadListDTO(BaseModel):
    parent_session_id: str
    items: list[ChildThreadSummaryDTO] = Field(default_factory=list)
    total: int
    teams: list[TeamBoardDTO] = Field(default_factory=list)


class SessionMainThreadDTO(BaseModel):
    """Session 唯一权威 main thread 解析结果（8.6 工作区合同）。"""

    session_id: str
    thread_id: str = Field(min_length=1)


class SessionInformationWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str


class SessionInformationSessionDTO(BaseModel):
    """信息面板直接投影同一 Session catalog 时间和导航、来源事实。"""

    session_id: str
    workspace_id: str
    title: str = Field(max_length=512)
    current_agent_id: str
    current_provider_id: str | None = None
    parent_session_id: str | None = Field(default=None, description="catalog 导航父链中最近祖先 Session 的稳定 ID；随导航移动派生，不表示物理父目录。")
    context_source_session_id: str | None = Field(default=None, description="创建 metadata 冻结的 fork 来源 Session 稳定 ID；独立于导航祖先，普通 Session 可为空。")
    kind: SessionKind
    created_at: datetime = Field(description="Session 创建 intent 冻结并由 catalog 投影的 UTC 创建时刻；导航改名和移动不重写。")
    updated_at: datetime = Field(description="catalog 记录的 Session 配置最近一次更新 UTC 时刻；初建等于 created_at，导航改名和移动不推进。")
    title_truncated: bool = False


class SessionInformationExecutionDTO(BaseModel):
    job_id: str | None = None
    status: str = "idle"
    current_tool: str | None = None
    last_error: str | None = None
    last_error_truncated: bool = False


class SessionInformationTraceDTO(BaseModel):
    observed_event_count: int = 0
    last_event_id: str | None = None
    last_event_type: str | None = None
    last_event_at: datetime | None = Field(default=None, description="本次已读取 Trace 中最后一项的来源事件时间；保留显式时区偏移，无 Trace 时为 null，不是诊断快照生成时刻。")
    truncated: bool = False


class SessionInformationResourceDTO(BaseModel):
    resource_id: str = Field(max_length=256)
    kind: SessionResourceKind
    name: str = Field(max_length=512)
    status: str = Field(max_length=128)
    updated_at: datetime
    ended_at: datetime | None = None
    name_truncated: bool = False


class SessionInformationResourceSummaryDTO(BaseModel):
    active: list[SessionInformationResourceDTO] = Field(
        default_factory=list,
        max_length=32,
    )
    recent_closed: list[SessionInformationResourceDTO] = Field(
        default_factory=list,
        max_length=16,
    )
    active_count: int = 0
    recent_closed_count: int = 0
    active_truncated: bool = False
    recent_closed_truncated: bool = False
    historical_omitted: bool = True


class SessionInformationRelationsDTO(BaseModel):
    child_count: int = 0
    child_ids: list[str] = Field(default_factory=list, max_length=32)
    child_ids_truncated: bool = False


class SessionInformationErrorDTO(BaseModel):
    event_id: str
    job_id: str
    type: str
    message: str = Field(max_length=2048)
    timestamp: datetime = Field(description="所选错误 Trace 的来源事件时间；保留其时刻与显式时区偏移，不在收集诊断信息时重新取时。")
    message_truncated: bool = False


class SessionInformationSnapshotDTO(BaseModel):
    kind: Literal["session_diagnostic_snapshot"] = "session_diagnostic_snapshot"
    schema_version: int = 2
    generated_at: datetime = Field(description="诊断读取完成后构造快照时取得的当前 UTC 时刻，接口按 ISO8601 表达；不等同于所含事件时间，也不代表跨 owner 的事务快照。")
    session: SessionInformationSessionDTO
    workspace: SessionInformationWorkspaceDTO
    execution: SessionInformationExecutionDTO
    trace: SessionInformationTraceDTO
    relations: SessionInformationRelationsDTO = Field(
        default_factory=SessionInformationRelationsDTO
    )
    resources: SessionInformationResourceSummaryDTO = Field(
        default_factory=SessionInformationResourceSummaryDTO
    )
    recent_errors: list[SessionInformationErrorDTO] = Field(
        default_factory=list,
        max_length=5,
    )


class DeleteSessionResultDTO(BaseModel):
    session_id: str
    status: str


class SessionControlResultDTO(BaseModel):
    session_id: str
    action: str
    status: str


class SessionInterruptResultDTO(BaseModel):
    session_id: str
    thread_id: CanonicalThreadId
    job_id: str
    status: str
    interrupt_request_id: str
    phase: str
    tool_name: str | None = None
    interrupted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SessionCompactResultDTO(BaseModel):
    session_id: str
    status: Literal["scheduled", "compacted", "skipped"]
    message: str
    before_message_count: int
    effective_message_count_before: int
    effective_message_count_after: int
    summarized_message_count: int
    retained_message_count: int
    summary: str | None = None
    strategy: Literal["cache_preserving", "cache_replacement"] | None = None
    compacted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SessionSkillUntrackRequest(BaseModel):
    """受信 Session untrack 请求：只接受 Skill 逻辑名，不接受路径。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Skill 逻辑名，不是文件路径。")


class SessionSkillUntrackErrorDTO(BaseModel):
    """untrack 确定性失败的闭合错误码与脱敏消息。"""

    code: str
    message: str


class SessionSkillUntrackResultDTO(BaseModel):
    """复用生产 CSM load_skill(mode=untrack) 的同一结果合同（脱敏 DTO）。

    不含物理路径、locator、credential 或正文；display_uri 只能是
    boxteam:// 虚拟资源 URI。
    """

    session_id: str
    thread_id: str
    name: str
    mode: Literal["untrack"]
    status: Literal[
        "loaded", "already_active", "not_tracked", "rebound", "error"
    ]
    display_uri: str | None = None
    revision: str | None = None
    content_hash: str | None = None
    append_status: Literal["appended", "already_active", "none"] = "none"
    tracked: bool = False
    queued: bool = False
    error: SessionSkillUntrackErrorDTO | None = None
