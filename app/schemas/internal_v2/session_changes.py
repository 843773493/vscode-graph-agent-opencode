from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


SessionChangesetKind = Literal["all", "turn"]
SessionFileChangeKind = Literal["create", "edit", "delete"]


class SessionChangesSummaryDTO(BaseModel):
    files: int = 0
    additions: int = 0
    deletions: int = 0


class SessionChangesetListItemDTO(BaseModel):
    changeset_id: str
    label: str
    description: str | None = None
    change_kind: SessionChangesetKind
    is_default: bool = False
    turn_id: str | None = None
    summary: SessionChangesSummaryDTO = Field(default_factory=SessionChangesSummaryDTO)


class SessionChangesetListDTO(BaseModel):
    session_id: str
    items: list[SessionChangesetListItemDTO]


class SessionFileChangeDTO(BaseModel):
    file_path: str
    kind: SessionFileChangeKind
    additions: int = 0
    deletions: int = 0
    reviewed: bool = False
    latest_edit_id: str
    tool_call_ids: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)
    turn_ids: list[str] = Field(default_factory=list)
    before_file: str | None = None
    after_file: str | None = None
    diff_file: str
    diff_text: str
    before_preview: str | None = None
    after_preview: str | None = None


class SessionChangesetDTO(BaseModel):
    session_id: str
    changeset_id: str
    label: str
    description: str | None = None
    change_kind: SessionChangesetKind
    turn_id: str | None = None
    status: Literal["ready"] = "ready"
    summary: SessionChangesSummaryDTO = Field(default_factory=SessionChangesSummaryDTO)
    files: list[SessionFileChangeDTO] = Field(default_factory=list)
    generated_at: datetime


class SessionFileReviewRequest(BaseModel):
    file_path: str
    reviewed: bool


class SessionFileReviewResultDTO(BaseModel):
    session_id: str
    file_path: str
    reviewed: bool
