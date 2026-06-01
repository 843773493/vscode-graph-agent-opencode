from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class WorkspaceIndexStatusDTO(BaseModel):
    status: str
    indexed_files: int = 0
    last_updated: Optional[str] = None


class WorkspaceIndexRebuildDTO(BaseModel):
    status: str
    job_id: str
