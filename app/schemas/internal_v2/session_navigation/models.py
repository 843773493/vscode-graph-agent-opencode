from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SessionCatalogNodeDTO(BaseModel):
    node_id: str
    kind: Literal["folder", "session"]
    name: str
    parent_node_id: str | None = None
    session_id: str | None = None
    folder_id: str | None = None
    has_children: bool = False
    storage_relative_path: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SessionCatalogPageDTO(BaseModel):
    revision: str
    parent_node_id: str | None = None
    items: list[SessionCatalogNodeDTO] = Field(default_factory=list)
    cursor: str | None = None
    total: int = 0
    consistency_warning: str | None = None


class SessionCatalogBreadcrumbDTO(BaseModel):
    revision: str
    items: list[SessionCatalogNodeDTO] = Field(default_factory=list)


class SessionCatalogSearchResultDTO(BaseModel):
    node: SessionCatalogNodeDTO
    breadcrumb: list[SessionCatalogNodeDTO] = Field(default_factory=list)
    relative_path: str


class SessionCatalogSearchResultsDTO(BaseModel):
    revision: str
    items: list[SessionCatalogSearchResultDTO] = Field(default_factory=list)
    cursor: str | None = None
    total: int = 0


class SessionCatalogExportDTO(BaseModel):
    revision: str
    items: list[SessionCatalogNodeDTO] = Field(default_factory=list)


class SessionFolderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    parent_folder_id: str | None = None


class SessionFolderUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_folder_id: str | None = None

    @model_validator(mode="after")
    def require_field(self) -> SessionFolderUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("会话文件夹更新至少需要一个字段")
        return self


class SessionFolderAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_id: str | None = None


class SessionCatalogNodeMoveRequest(BaseModel):
    """把会话目录节点移动到根、文件夹或会话 children 边界。"""

    model_config = ConfigDict(extra="forbid")

    parent_node_id: str | None = Field(...)


class SessionGenerationGeneratorTypeDTO(BaseModel):
    type_id: str
    version: str


class SessionGenerationPlacementDTO(BaseModel):
    kind: Literal["workspace", "session", "session_folder"]
    workspace_id: str
    session_id: str | None = None
    folder_id: str | None = None

    @model_validator(mode="after")
    def validate_target(self) -> SessionGenerationPlacementDTO:
        if self.kind == "session" and self.session_id is None:
            raise ValueError("session placement 缺少 session_id")
        if self.kind == "session_folder" and self.folder_id is None:
            raise ValueError("session_folder placement 缺少 folder_id")
        return self


class SessionGenerationContextSourceDTO(BaseModel):
    kind: Literal["fresh", "live_session", "snapshot"] = "fresh"
    workspace_id: str | None = None
    session_id: str | None = None
    snapshot_id: str | None = None


class SessionGenerationTargetDTO(BaseModel):
    workspace_id: str
    session_id: str


class SessionGenerationStrategyDTO(BaseModel):
    mode: Literal[
        "new_per_run",
        "continue_existing",
        "fork_new_and_report_back",
    ]
    target: SessionGenerationTargetDTO | None = None
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
    def validate_target(self) -> SessionGenerationStrategyDTO:
        if self.mode == "new_per_run" and self.target is not None:
            raise ValueError("new_per_run strategy 不允许 target")
        if self.mode != "new_per_run" and self.target is None:
            raise ValueError(f"{self.mode} strategy 缺少 target")
        return self


class SessionGenerationExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    generator_id: str
    idempotency_key: str
    generator_type: SessionGenerationGeneratorTypeDTO
    name: str
    config: dict[str, object] = Field(default_factory=dict)
    placement: SessionGenerationPlacementDTO
    context_source: SessionGenerationContextSourceDTO
    session_strategy: SessionGenerationStrategyDTO
    title: str
    navigation_path: list[str] = Field(default_factory=list)
    execution_workspace_id: str


class SessionGenerationOutputDTO(BaseModel):
    kind: Literal["session"] = "session"
    workspace_id: str
    session_id: str
    title: str | None = None
    navigation_path: list[str] = Field(default_factory=list)
    storage_relative_path: str | None = None


class SessionGenerationExecuteResultDTO(BaseModel):
    run_id: str
    status: Literal["completed", "queued", "reporting", "failed", "skipped"]
    outputs: list[SessionGenerationOutputDTO] = Field(default_factory=list)
    message_id: str | None = None
    job_id: str | None = None
    report_back_job_id: str | None = None
    error: str | None = None


class SessionGenerationCapabilityDTO(BaseModel):
    type_id: str
    supported_versions: list[str] = Field(default_factory=list)
    config_schema: dict[str, object]


class SessionGenerationCapabilitiesDTO(BaseModel):
    items: list[SessionGenerationCapabilityDTO] = Field(default_factory=list)
