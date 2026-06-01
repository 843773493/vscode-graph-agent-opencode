from datetime import datetime
from typing import Any, Optional
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
