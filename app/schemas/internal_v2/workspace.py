from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceDirectoryRevisionDTO,
    WorkspaceFileListDTO,
    WorkspaceFileMutationIntentDTO,
)


class WorkspaceDTO(BaseModel):
    workspace_id: str
    root_path: str
    name: str
    project_type: str | None = None
    git: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)


class WorkspaceContextDTO(BaseModel):
    workspace_id: str
    root_path: str
    project_type: str | None = None
    languages: list[str] = Field(default_factory=list)
    git: dict[str, Any] = Field(default_factory=dict)
    index_status: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class WorkspaceIndexStatusDTO(BaseModel):
    status: str
    indexed_files: int = 0
    last_updated: str | None = None


class WorkspaceIndexRebuildDTO(BaseModel):
    status: str
    job_id: str


WorkspaceFileScope = Literal["workspace", "filesystem"]
FileTreeShortcutSource = Literal["session", "workspace"]


class WorkspaceFileContentDTO(BaseModel):
    root_path: str
    path: str
    name: str
    content: str
    language: str
    size: int
    modified_at: str | None = None
    revision: str


class WorkspaceFileUpdateRequest(BaseModel):
    content: str
    expected_revision: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class WorkspaceFileCreateRequest(BaseModel):
    intent: WorkspaceFileMutationIntentDTO
    name: str = Field(min_length=1, max_length=255)
    kind: Literal["file", "directory"]


class WorkspaceFilePasteRequest(BaseModel):
    intent: WorkspaceFileMutationIntentDTO
    source_paths: list[str] = Field(min_length=1, max_length=100)


class WorkspaceFileCopyRequest(BaseModel):
    intent: WorkspaceFileMutationIntentDTO
    source_path: str = Field(min_length=1, max_length=4096)
    source_scope: WorkspaceFileScope = "workspace"


class WorkspaceFileWatchRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=100)
    after_event_id: int | None = Field(default=None, ge=0)


class WorkspaceFileChangeDTO(BaseModel):
    kind: Literal["create", "edit", "delete"]
    path: str = Field(min_length=1)


class WorkspaceFileChangeBatchDTO(BaseModel):
    changes: list[WorkspaceFileChangeDTO]
    overflow: bool
    directories: list[WorkspaceDirectoryRevisionDTO] = Field(default_factory=list)
    event_id: int = Field(default=0, ge=0)
    invalidated_roots: list[str] = Field(default_factory=list)
    snapshots: list[WorkspaceFileListDTO] = Field(default_factory=list)


class WorkspaceFileRevealDTO(BaseModel):
    path: str


class FileTreeShortcutRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    label: str | None = Field(default=None, min_length=1, max_length=200)


class FileTreeShortcutDTO(BaseModel):
    path: str
    label: str
    source: FileTreeShortcutSource


class SessionFileTreeSettingsDTO(BaseModel):
    session_id: str
    session_shortcuts: list[FileTreeShortcutDTO] = Field(default_factory=list)
    workspace_shortcuts: list[FileTreeShortcutDTO] = Field(default_factory=list)
    default_shortcuts: list[FileTreeShortcutDTO] = Field(default_factory=list)
    effective_shortcuts: list[FileTreeShortcutDTO] = Field(default_factory=list)
