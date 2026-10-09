from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.session_catalog_store.contracts import (
    SessionGenerationOriginDTO,
    SessionKind,
)

from .common import TimestampedDTO
from .session_context import SessionContextScope, SessionResourceIdentity
from .session_resource import SessionResourceKind

TitleSource = Literal["default", "user", "auto"]
SessionForkMode = Literal["context_fork", "history_prefix_fork", "full_rollout_copy"]
ChildThreadStatus = Literal["pending", "running", "completed", "failed"]


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = "新会话"
    agent_id: str | None = None
    folder_id: str | None = None


class SessionForkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: SessionForkMode = "context_fork"
    turn_id: str | None = None
    anchor_mode: Literal["inclusive", "before"] = "inclusive"
    pinned: bool = False
    place_under_source: bool = False


class SessionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = None
    provider_id: str | None = None


class SessionDTO(TimestampedDTO):
    session_id: str
    workspace_id: str
    resource_identity: SessionResourceIdentity
    uri: str
    scope: SessionContextScope
    title: str
    title_source: TitleSource = "default"
    current_agent_id: str
    current_provider_id: str | None = None
    parent_session_id: str | None = None
    context_source_session_id: str | None = None
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
    status 是面向调用方的唯一运行状态；admission_state 与
    collaboration_state 继续保留为诊断字段。
    """

    thread_id: str
    created_at: datetime
    delegation_id: str | None = None
    role: str | None = None
    subagent_type: str | None = None
    title: str | None = None
    collaboration_state: str | None = None
    admission_state: str | None = None
    status: ChildThreadStatus
    runtime: ChildThreadRuntimeDTO | None = None
    report_execution_id: str | None = None
    report_status: str | None = None


class ChildThreadListDTO(BaseModel):
    parent_session_id: str
    items: list[ChildThreadSummaryDTO] = Field(default_factory=list)
    total: int


class SessionMainThreadDTO(BaseModel):
    """Session 唯一权威 main thread 解析结果（8.6 工作区合同）。"""

    session_id: str
    thread_id: str = Field(min_length=1)


class SessionInformationWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str


class SessionInformationSessionDTO(BaseModel):
    session_id: str
    workspace_id: str
    title: str = Field(max_length=512)
    current_agent_id: str
    current_provider_id: str | None = None
    parent_session_id: str | None = None
    context_source_session_id: str | None = None
    kind: SessionKind
    created_at: datetime
    updated_at: datetime
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
    last_event_at: datetime | None = None
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
    timestamp: datetime
    message_truncated: bool = False


class SessionInformationSnapshotDTO(BaseModel):
    kind: Literal["session_diagnostic_snapshot"] = "session_diagnostic_snapshot"
    schema_version: int = 2
    generated_at: datetime
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
    history_file_path: str | None = None
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
