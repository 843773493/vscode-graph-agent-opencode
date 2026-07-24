from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from croniter import croniter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WorkspaceNavigationNodeKind = Literal["workspace_folder", "workspace_ref"]


class WorkspaceNavigationNodeDTO(BaseModel):
    node_id: str
    kind: WorkspaceNavigationNodeKind
    name: str
    parent_node_id: str | None = None
    workspace_id: str | None = None
    position: int = 0

    @model_validator(mode="after")
    def validate_target(self) -> "WorkspaceNavigationNodeDTO":
        if self.kind == "workspace_ref" and self.workspace_id is None:
            raise ValueError("workspace_ref 缺少 workspace_id")
        if self.kind == "workspace_folder" and self.workspace_id is not None:
            raise ValueError("workspace_folder 不能包含 workspace_id")
        return self


class WorkspaceNavigationTreeDTO(BaseModel):
    revision: str
    nodes: list[WorkspaceNavigationNodeDTO] = Field(default_factory=list)


class WorkspaceNavigationBreadcrumbDTO(BaseModel):
    revision: str
    items: list[WorkspaceNavigationNodeDTO] = Field(default_factory=list)


class WorkspaceFolderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    parent_node_id: str | None = None
    position: int | None = None


class WorkspaceNavigationNodeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_node_id: str | None = None
    position: int | None = None

    @model_validator(mode="after")
    def require_field(self) -> "WorkspaceNavigationNodeUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("导航节点更新至少需要一个字段")
        return self


class WorkspaceNavigationReorderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_node_id: str | None = None
    node_ids: list[str] = Field(min_length=1)


class SessionLocatorDTO(BaseModel):
    workspace_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class GeneratorTypeRefDTO(BaseModel):
    type_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class GeneratorTriggerDTO(BaseModel):
    type: Literal["manual", "cron", "interval"] = "manual"
    expression: str | None = None
    interval_seconds: int | None = Field(default=None, ge=1)
    timezone: str = "UTC"

    @model_validator(mode="after")
    def validate_trigger(self) -> "GeneratorTriggerDTO":
        if self.type == "cron" and not self.expression:
            raise ValueError("cron trigger 缺少 expression")
        if self.type == "cron" and self.expression and not croniter.is_valid(self.expression):
            raise ValueError(f"cron expression 非法: {self.expression}")
        if self.type == "interval" and self.interval_seconds is None:
            raise ValueError("interval trigger 缺少 interval_seconds")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"未知时区: {self.timezone}") from error
        return self


class GeneratorPlacementDTO(BaseModel):
    kind: Literal["workspace", "session", "session_folder"]
    workspace_id: str = Field(min_length=1)
    session_id: str | None = None
    folder_id: str | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "GeneratorPlacementDTO":
        if self.kind == "session" and self.session_id is None:
            raise ValueError("session placement 缺少 session_id")
        if self.kind == "session_folder" and self.folder_id is None:
            raise ValueError("session_folder placement 缺少 folder_id")
        return self

class GeneratorContextSourceDTO(BaseModel):
    kind: Literal["fresh", "live_session", "snapshot"] = "fresh"
    workspace_id: str | None = None
    session_id: str | None = None
    snapshot_id: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "GeneratorContextSourceDTO":
        if self.kind == "live_session" and (
            self.workspace_id is None or self.session_id is None
        ):
            raise ValueError("live_session context source 缺少 workspace_id/session_id")
        if self.kind == "snapshot" and self.snapshot_id is None:
            raise ValueError("snapshot context source 缺少 snapshot_id")
        return self


class GeneratorNamingDTO(BaseModel):
    title_template: str = Field(default="{generator.name}", min_length=1, max_length=500)
    path_template: list[str] = Field(
        default_factory=lambda: ["{generator.name}", "{generated_at:yyyy-MM-dd}"],
        max_length=20,
    )


class GeneratorSessionStrategyDTO(BaseModel):
    mode: Literal[
        "new_per_run",
        "continue_existing",
        "fork_new_and_report_back",
    ] = "new_per_run"
    target: SessionLocatorDTO | None = None
    concurrency: Literal["queue"] = "queue"
    report_back: Literal[
        "none",
        "link",
        "summary",
        "summary_and_link",
        "full",
        "continue_agent",
    ] = "none"

    @model_validator(mode="after")
    def validate_target(self) -> "GeneratorSessionStrategyDTO":
        if self.mode == "new_per_run" and self.target is not None:
            raise ValueError("new_per_run strategy 不允许 target")
        if self.mode != "new_per_run" and self.target is None:
            raise ValueError(f"{self.mode} strategy 缺少 target")
        return self


class GeneratorPoliciesDTO(BaseModel):
    overlap: Literal["allow"] = "allow"
    misfire: Literal["skip", "run_latest", "catch_up"] = "skip"
    mount_missing: Literal["pause", "fail"] = "pause"
    delete_outputs: Literal["keep", "cascade"] = "keep"


class GeneratorUIPolicyDTO(BaseModel):
    on_run_started: Literal["stay", "open_generated"] = "stay"
    on_run_completed: Literal["none", "notify", "open_generated", "open_on_failure"] = (
        "notify"
    )


class GeneratorDefinitionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    generator_type: GeneratorTypeRefDTO = Field(
        default_factory=lambda: GeneratorTypeRefDTO(
            type_id="builtin.agent_prompt",
            version="1",
        )
    )
    enabled: bool = True
    trigger: GeneratorTriggerDTO = Field(default_factory=GeneratorTriggerDTO)
    placement: GeneratorPlacementDTO
    execution_workspace_id: str = Field(min_length=1)
    context_source: GeneratorContextSourceDTO = Field(
        default_factory=GeneratorContextSourceDTO
    )
    created_from: SessionLocatorDTO | None = None
    naming: GeneratorNamingDTO = Field(default_factory=GeneratorNamingDTO)
    session_strategy: GeneratorSessionStrategyDTO = Field(
        default_factory=GeneratorSessionStrategyDTO
    )
    policies: GeneratorPoliciesDTO = Field(default_factory=GeneratorPoliciesDTO)
    ui_policy: GeneratorUIPolicyDTO = Field(default_factory=GeneratorUIPolicyDTO)
    config: dict[str, object] = Field(default_factory=dict)


class GeneratorDefinitionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    trigger: GeneratorTriggerDTO | None = None
    placement: GeneratorPlacementDTO | None = None
    execution_workspace_id: str | None = Field(default=None, min_length=1)
    context_source: GeneratorContextSourceDTO | None = None
    naming: GeneratorNamingDTO | None = None
    session_strategy: GeneratorSessionStrategyDTO | None = None
    policies: GeneratorPoliciesDTO | None = None
    ui_policy: GeneratorUIPolicyDTO | None = None
    config: dict[str, object] | None = None

    @model_validator(mode="after")
    def require_field(self) -> "GeneratorDefinitionUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("生成器更新至少需要一个字段")
        return self


class GeneratorDefinitionDTO(GeneratorDefinitionCreateRequest):
    generator_id: str
    status: Literal["ready", "paused", "blocked"] = "ready"
    status_reason: str | None = None
    revision: int = 1
    created_at: datetime
    updated_at: datetime


class GeneratorDefinitionListDTO(BaseModel):
    revision: str
    items: list[GeneratorDefinitionDTO] = Field(default_factory=list)


class GeneratorPlacementPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    naming: GeneratorNamingDTO
    session_title: str = Field(default="生成会话", min_length=1, max_length=500)
    generated_at: datetime | None = None
    placement: GeneratorPlacementDTO | None = None
    session_strategy: GeneratorSessionStrategyDTO = Field(
        default_factory=GeneratorSessionStrategyDTO
    )


class GeneratorPlacementPreviewDTO(BaseModel):
    preview_kind: Literal["logical_physical_path_template"] = (
        "logical_physical_path_template"
    )
    title: str
    path_segments: list[str]
    session_path_segment: str
    relative_path: str


GenerationRunStatus = Literal[
    "planned",
    "dispatching",
    "running",
    "reporting",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "skipped",
]


class GenerationOutputDTO(BaseModel):
    kind: Literal["session"] = "session"
    workspace_id: str
    session_id: str
    title: str | None = None
    navigation_path: list[str] = Field(default_factory=list)
    storage_relative_path: str | None = None


class GenerationRunDTO(BaseModel):
    run_id: str
    generator_id: str
    idempotency_key: str
    status: GenerationRunStatus
    trigger_type: str
    scheduled_for: datetime
    outputs: list[GenerationOutputDTO] = Field(default_factory=list)
    execution_workspace_id: str | None = None
    message_id: str | None = None
    job_id: str | None = None
    report_back_job_id: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class GenerationRunListDTO(BaseModel):
    items: list[GenerationRunDTO] = Field(default_factory=list)


class GeneratorManualRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str | None = Field(default=None, min_length=1, max_length=500)


class GatewaySessionSearchMatchDTO(BaseModel):
    workspace_id: str
    workspace_name: str
    node_id: str
    node_kind: Literal["workspace_folder", "workspace", "folder", "session"]
    name: str
    session_id: str | None = None
    relative_path: str
    storage_relative_path: str | None = None
    breadcrumb_names: list[str] = Field(default_factory=list)
    breadcrumb_node_ids: list[str] = Field(default_factory=list)


class GatewaySessionSearchWorkspaceStatusDTO(BaseModel):
    workspace_id: str
    workspace_name: str
    status: Literal["available", "stale", "unavailable"]
    error: str | None = None


class GatewaySessionSearchResultsDTO(BaseModel):
    items: list[GatewaySessionSearchMatchDTO] = Field(default_factory=list)
    workspaces: list[GatewaySessionSearchWorkspaceStatusDTO] = Field(default_factory=list)
    total: int = 0
