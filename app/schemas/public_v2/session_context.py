from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


SessionContextConsistency = Literal["not_checked", "matched", "changed"]


class SessionContextSnapshotMetadataDTO(BaseModel):
    snapshot_id: str
    content_sha256: str
    generated_at: str
    line_count: int
    raw_message_count: int
    byte_count: int
    compacted: bool
    compaction_cutoff: int | None = None
    history_file_path: str | None = None
    expected_snapshot_id: str | None = None
    consistency: SessionContextConsistency
    warning: str | None = None


class SessionRecentUserTextMessageDTO(BaseModel):
    role: Literal["user"] = "user"
    text: str


class SessionRecentAssistantTextMessageDTO(BaseModel):
    role: Literal["assistant"] = "assistant"
    type: Literal["text"] = "text"
    text: str


SessionRecentTextMessageDTO = (
    SessionRecentUserTextMessageDTO | SessionRecentAssistantTextMessageDTO
)


class SessionRecentTextMessagesDTO(BaseModel):
    session_id: str
    rounds: int
    user_message_count: int
    context_snapshot: SessionContextSnapshotMetadataDTO
    messages: list[SessionRecentTextMessageDTO] = Field(default_factory=list)


class SessionContextGrepRequest(BaseModel):
    pattern: str = Field(min_length=1, description="Python 正则表达式")
    case_sensitive: bool = False
    max_matches: int = Field(default=20, ge=1, le=200)
    expected_snapshot_id: str | None = None


class SessionContextMatchDTO(BaseModel):
    line_number: int
    match_start: int
    match_end: int
    preview: str
    preview_truncated_left: bool
    preview_truncated_right: bool
    line_sha256: str


class SessionContextGrepResultDTO(BaseModel):
    session_id: str
    pattern: str
    case_sensitive: bool
    context_snapshot: SessionContextSnapshotMetadataDTO
    total_matching_lines: int
    returned_match_count: int
    matches_truncated: bool
    matches: list[SessionContextMatchDTO] = Field(default_factory=list)


class SessionContextLineDTO(BaseModel):
    line_number: int
    text: str
    original_chars: int
    truncated: bool
    line_sha256: str


class SessionContextReadResultDTO(BaseModel):
    session_id: str
    context_snapshot: SessionContextSnapshotMetadataDTO
    line_start: int
    line_end: int
    has_more: bool
    next_line_start: int | None = None
    lines: list[SessionContextLineDTO] = Field(default_factory=list)
