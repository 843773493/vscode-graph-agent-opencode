from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import TimestampedDTO
from .session_resource import SessionResourceKind

TitleSource = Literal["default", "user", "auto"]
SessionKind = Literal["normal", "context_fork", "delegated"]
SessionForkMode = Literal["context_fork", "history_prefix_fork", "full_rollout_copy"]
DelegationStartStatus = Literal["pending", "running", "failed"]


class SessionDelegationDTO(BaseModel):
    parent_session_id: str
    parent_job_id: str
    parent_tool_call_id: str
    subagent_type: str
    start_status: DelegationStartStatus = "pending"
    start_error: Optional[str] = None


class SessionGenerationOriginDTO(BaseModel):
    """会话由通用生成器创建时的不可变来源。"""

    generator_id: str
    run_id: str
    idempotency_key: str
    generator_type_id: str
    generator_type_version: str


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = "新会话"
    agent_id: Optional[str] = None
    title_source: Optional[TitleSource] = None
    folder_id: Optional[str] = None


class SessionForkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: SessionForkMode = "context_fork"
    turn_id: str | None = None
    anchor_mode: Literal["inclusive", "before"] = "inclusive"
    pinned: bool = False


class SessionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    agent_id: Optional[str] = None
    provider_id: Optional[str] = None
    title_source: Optional[TitleSource] = None


class SessionDTO(TimestampedDTO):
    session_id: str
    workspace_id: str
    title: str
    title_source: TitleSource = "default"
    current_agent_id: str
    current_provider_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    context_source_session_id: Optional[str] = None
    kind: SessionKind = "normal"
    delegation: Optional[SessionDelegationDTO] = None
    generation_origin: Optional[SessionGenerationOriginDTO] = None

    @model_validator(mode="after")
    def validate_internal_origin(self) -> Self:
        if self.kind == "delegated" and self.delegation is None:
            raise ValueError("delegated 会话缺少不可变 delegation 来源")
        if self.kind != "delegated" and self.delegation is not None:
            raise ValueError("只有 delegated 会话可以包含 delegation 来源")
        if self.kind == "context_fork" and self.context_source_session_id is None:
            raise ValueError("context_fork 会话缺少 context_source_session_id")
        return self


class SessionListResultDTO(BaseModel):
    items: list[SessionDTO]
    total: int
    cursor: Optional[str] = None


class SessionInformationWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str
    root_path: str


class SessionInformationSessionDTO(BaseModel):
    session_id: str
    workspace_id: str
    title: str = Field(max_length=512)
    current_agent_id: str
    current_provider_id: Optional[str] = None
    parent_session_id: Optional[str] = None
    context_source_session_id: Optional[str] = None
    kind: SessionKind
    created_at: datetime
    updated_at: datetime
    title_truncated: bool = False


class SessionInformationExecutionDTO(BaseModel):
    job_id: Optional[str] = None
    status: str = "idle"
    current_tool: Optional[str] = None
    last_error: Optional[str] = None
    last_error_truncated: bool = False


class SessionInformationTraceDTO(BaseModel):
    observed_event_count: int = 0
    last_event_id: Optional[str] = None
    last_event_type: Optional[str] = None
    last_event_at: Optional[datetime] = None
    truncated: bool = False


class SessionInformationResourceDTO(BaseModel):
    resource_id: str = Field(max_length=256)
    kind: SessionResourceKind
    name: str = Field(max_length=512)
    status: str = Field(max_length=128)
    updated_at: datetime
    ended_at: Optional[datetime] = None
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
    storage_path: str
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
    cleaned_execution_runs: int = Field(
        default=0,
        description="删除会话时清理的一次性 agent 执行记录数量；这些记录不属于后台连接。",
    )
    cleaned_background_tasks: int = 0
    cleaned_terminals: int = 0


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
    tool_name: Optional[str] = None
    interrupted_at: datetime = Field(default_factory=lambda: datetime.now())


class SessionCompactResultDTO(BaseModel):
    session_id: str
    status: Literal["scheduled", "compacted", "skipped"]
    message: str
    before_message_count: int
    effective_message_count_before: int
    effective_message_count_after: int
    summarized_message_count: int
    retained_message_count: int
    summary: Optional[str] = None
    history_file_path: Optional[str] = None
    strategy: Optional[Literal["cache_preserving", "cache_replacement"]] = None
    compacted_at: datetime = Field(default_factory=lambda: datetime.now())
