from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class WorkspaceDTO(BaseModel):
    workspace_id: str
    root_path: str
    name: str
    project_type: Optional[str] = None
    git: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)


class WorkspaceContextDTO(BaseModel):
    workspace_id: str
    root_path: str
    project_type: Optional[str] = None
    languages: list[str] = Field(default_factory=list)
    git: dict[str, Any] = Field(default_factory=dict)
    index_status: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class WorkspaceIndexStatusDTO(BaseModel):
    status: str
    indexed_files: int = 0
    last_updated: Optional[str] = None


class WorkspaceIndexRebuildDTO(BaseModel):
    status: str
    job_id: str


WorkspaceFileKind = Literal["file", "directory", "symlink", "other"]
WorkspaceFileScope = Literal["workspace", "filesystem"]


class WorkspaceFileNodeDTO(BaseModel):
    name: str
    path: str
    kind: WorkspaceFileKind
    has_children: bool = False
    size: int | None = None
    modified_at: str | None = None


class WorkspaceFileListDTO(BaseModel):
    root_path: str
    path: str
    items: list[WorkspaceFileNodeDTO] = Field(default_factory=list)
    truncated: bool = False
    limit: int = 500
    next_cursor: str | None = None


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
    name: str = Field(min_length=1, max_length=255)
    kind: Literal["file", "directory"]


class WorkspaceFilePasteRequest(BaseModel):
    source_paths: list[str] = Field(min_length=1, max_length=100)


class WorkspaceFileWatchRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=100)


class WorkspaceFileChangeDTO(BaseModel):
    kind: Literal["create", "edit", "delete"]
    path: str = Field(min_length=1)


class WorkspaceFileChangeBatchDTO(BaseModel):
    changes: list[WorkspaceFileChangeDTO]
    overflow: bool


class WorkspaceFileRevealDTO(BaseModel):
    path: str


class FileTreeShortcutRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    label: str | None = Field(default=None, min_length=1, max_length=200)


class FileTreeShortcutDTO(BaseModel):
    path: str
    label: str
    source: Literal["session", "workspace"]


class SessionFileTreeSettingsDTO(BaseModel):
    session_id: str
    session_shortcuts: list[FileTreeShortcutDTO] = Field(default_factory=list)
    workspace_shortcuts: list[FileTreeShortcutDTO] = Field(default_factory=list)
    default_shortcuts: list[FileTreeShortcutDTO] = Field(default_factory=list)
    effective_shortcuts: list[FileTreeShortcutDTO] = Field(default_factory=list)
