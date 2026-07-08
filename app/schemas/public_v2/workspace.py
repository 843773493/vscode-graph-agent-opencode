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


class WorkspaceFileContentDTO(BaseModel):
    root_path: str
    path: str
    name: str
    content: str
    language: str
    size: int
    modified_at: str | None = None
