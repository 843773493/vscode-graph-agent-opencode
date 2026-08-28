from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


SessionContextView = Literal[
    "overview",
    "messages",
    "records",
    "information",
    "inventory",
]
SessionContextInclude = Literal[
    "visible_text",
    "reasoning",
    "tool_summary",
    "tool_calls",
    "tool_results",
    "system",
    "raw_record",
]
SessionContextSearchSource = Literal[
    "effective_context",
    "session_catalog",
    "session_information",
]
SessionContextMatchMode = Literal["literal", "regex"]


class SessionContextReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str = Field(min_length=1)
    view: SessionContextView = "overview"
    include: list[SessionContextInclude] = Field(
        default_factory=lambda: ["visible_text", "tool_summary"]
    )
    recent_rounds: int = Field(default=3, ge=1, le=50)
    include_initial_goal: bool = True
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=200)
    max_chars: int = Field(default=16_384, ge=256, le=65_536)
    expected_revision: str | None = None


class SessionContextSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str = Field(min_length=1)
    query: str = Field(min_length=1)
    sources: list[SessionContextSearchSource] = Field(
        default_factory=lambda: ["effective_context"]
    )
    match_mode: SessionContextMatchMode = "literal"
    case_sensitive: bool = False
    max_results: int = Field(default=20, ge=1, le=200)
    max_chars: int = Field(default=16_384, ge=256, le=65_536)
    cursor: str | None = None
    expected_revision: str | None = None


class SessionContextItemDTO(BaseModel):
    kind: str
    locator: str
    role: str | None = None
    record_index: int | None = None
    text: str | None = None
    reasoning: str | None = None
    tool_summary: list[str] = Field(default_factory=list)
    tool_calls: list[dict[str, object]] = Field(default_factory=list)
    tool_results: list[dict[str, object]] = Field(default_factory=list)
    data: dict[str, object] | None = None
    raw_record: dict[str, object] | None = None
    truncated: bool = False


class SessionContextPartialErrorDTO(BaseModel):
    resource: str
    error: str


class SessionContextReadResultDTO(BaseModel):
    resource: str
    view: SessionContextView
    revision: str
    compacted: bool = False
    compaction_cutoff: int | None = None
    raw_message_count: int = 0
    effective_record_count: int = 0
    returned_chars: int = 0
    truncated: bool = False
    has_more: bool = False
    next_cursor: str | None = None
    items: list[SessionContextItemDTO] = Field(default_factory=list)
    partial_errors: list[SessionContextPartialErrorDTO] = Field(default_factory=list)
    omitted_partial_error_count: int = 0


class SessionContextSearchMatchDTO(BaseModel):
    locator: str
    preview: str
    source: SessionContextSearchSource
    revision: str
    record_index: int | None = None
    match_start: int
    match_end: int


class SessionContextSearchResultDTO(BaseModel):
    resource: str
    query: str
    match_mode: SessionContextMatchMode
    revision: str
    returned_chars: int = 0
    truncated: bool = False
    has_more: bool = False
    next_cursor: str | None = None
    total_matches: int = 0
    matches: list[SessionContextSearchMatchDTO] = Field(default_factory=list)
    partial_errors: list[SessionContextPartialErrorDTO] = Field(default_factory=list)
    omitted_partial_error_count: int = 0
