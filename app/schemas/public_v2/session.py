from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import TimestampedDTO
from .session_resource import SessionResourceKind

TitleSource = Literal["default", "user", "auto"]
SessionKind = Literal["normal", "context_fork", "delegated"]
DelegationStartStatus = Literal["pending", "running", "failed"]


class SessionDelegationDTO(BaseModel):
    parent_session_id: str
    parent_job_id: str
    parent_tool_call_id: str
    subagent_type: str
    start_status: DelegationStartStatus = "pending"
    start_error: Optional[str] = None


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = "新会话"
    agent_id: Optional[str] = None
    title_source: Optional[TitleSource] = None


class SessionUpdateRequest(BaseModel):
    title: Optional[str] = None
    agent_id: Optional[str] = None
    title_source: Optional[TitleSource] = None
    parent_session_id: Optional[str] = None


class SessionDTO(TimestampedDTO):
    session_id: str
    workspace_id: str
    title: str
    title_source: TitleSource = "default"
    current_agent_id: str
    parent_session_id: Optional[str] = None
    kind: SessionKind = "normal"
    delegation: Optional[SessionDelegationDTO] = None

    @model_validator(mode="after")
    def validate_internal_origin(self) -> Self:
        if self.kind == "delegated" and self.delegation is None:
            raise ValueError("delegated 会话缺少不可变 delegation 来源")
        if self.kind != "delegated" and self.delegation is not None:
            raise ValueError("只有 delegated 会话可以包含 delegation 来源")
        if self.kind == "context_fork" and self.parent_session_id is None:
            raise ValueError("context_fork 会话缺少 parent_session_id")
        return self


class SessionListResultDTO(BaseModel):
    items: list[SessionDTO]
    total: int
    cursor: Optional[str] = None


class SessionInformationWorkspaceDTO(BaseModel):
    workspace_id: str
    name: str
    root_path: str


class SessionInformationExecutionDTO(BaseModel):
    job_id: Optional[str] = None
    status: str = "idle"
    current_tool: Optional[str] = None
    last_error: Optional[str] = None


class SessionInformationTraceDTO(BaseModel):
    event_count: int = 0
    last_event_id: Optional[str] = None
    last_event_type: Optional[str] = None
    last_event_at: Optional[datetime] = None


class SessionInformationResourceDTO(BaseModel):
    resource_id: str
    kind: SessionResourceKind
    name: str
    status: str
    updated_at: datetime


class SessionInformationErrorDTO(BaseModel):
    event_id: str
    job_id: str
    type: str
    message: str
    timestamp: datetime


class SessionInformationSnapshotDTO(BaseModel):
    kind: Literal["boxteam_session_information"] = "boxteam_session_information"
    schema_version: int = 1
    generated_at: datetime
    session: SessionDTO
    child_session_ids: list[str] = Field(default_factory=list)
    workspace: SessionInformationWorkspaceDTO
    storage_path: str
    execution: SessionInformationExecutionDTO
    trace: SessionInformationTraceDTO
    resources: list[SessionInformationResourceDTO] = Field(default_factory=list)
    recent_errors: list[SessionInformationErrorDTO] = Field(default_factory=list)


class SessionAutoContinueStartRequest(BaseModel):
    poll_interval_seconds: float = 1.0


class SessionAutoContinueStatusDTO(BaseModel):
    session_id: str
    enabled: bool
    task_id: Optional[str]
    task_status: str
    poll_interval_seconds: Optional[float]
    started_at: Optional[datetime]
    forwarded_count: int
    last_forwarded_at: Optional[datetime]
    last_trigger_event_id: Optional[str]
    last_trigger_job_id: Optional[str]
    last_enqueued_job_id: Optional[str]


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
