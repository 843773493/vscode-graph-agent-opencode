from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceIdentity,
    validate_resource_identity,
)

SessionContextScope = Literal["workspace", "user", "gateway", "inline"]
SessionContextView = Literal[
    "overview",
    "messages",
    "records",
    "information",
    "inventory",
    "assembly",
    "assemblies",
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
SessionResourceIdentity = Annotated[
    ResourceIdentity,
    AfterValidator(validate_resource_identity),
]


class SessionContextReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_identity: SessionResourceIdentity
    uri: str = Field(min_length=1)
    scope: SessionContextScope
    view: SessionContextView = "overview"
    record_index: int | None = Field(default=None, ge=0)
    assembly_ref: SessionResourceIdentity | None = None
    include: list[SessionContextInclude] = Field(
        default_factory=lambda: ["visible_text", "tool_summary"]
    )
    recent_rounds: int = Field(default=3, ge=1, le=50)
    include_initial_goal: bool = True
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=200, description="投影 items 的单页条数上限；messages 中用户与助手消息分别计条，max_chars 可进一步截短。不是 recent_rounds、Turn 数或完整 snapshot，续页由 has_more/next_cursor 判定。")
    max_chars: int = Field(default=16_384, ge=256, le=65_536)
    revision: str | None = None


class SessionContextSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_identity: SessionResourceIdentity
    uri: str = Field(min_length=1)
    scope: SessionContextScope
    query: str = Field(min_length=1)
    sources: list[SessionContextSearchSource] = Field(
        default_factory=lambda: ["effective_context"]
    )
    match_mode: SessionContextMatchMode = "literal"
    case_sensitive: bool = False
    max_results: int = Field(default=20, ge=1, le=200)
    max_chars: int = Field(default=16_384, ge=256, le=65_536)
    cursor: str | None = None
    revision: str | None = None


class SessionContextItemDTO(BaseModel):
    kind: str
    resource_identity: SessionResourceIdentity | None = None
    uri: str | None = None
    scope: SessionContextScope | None = None
    assembly_ref: SessionResourceIdentity | None = None
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
    resource_identity: SessionResourceIdentity
    error: str


class SessionContextReadResultDTO(BaseModel):
    resource_identity: SessionResourceIdentity
    uri: str
    scope: SessionContextScope
    view: SessionContextView
    record_index: int | None = None
    assembly_ref: SessionResourceIdentity | None = None
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
    resource_identity: SessionResourceIdentity
    uri: str
    scope: SessionContextScope
    preview: str
    source: SessionContextSearchSource
    revision: str
    record_index: int | None = None
    match_start: int
    match_end: int


class SessionContextSearchResultDTO(BaseModel):
    resource_identity: SessionResourceIdentity
    uri: str
    scope: SessionContextScope
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
