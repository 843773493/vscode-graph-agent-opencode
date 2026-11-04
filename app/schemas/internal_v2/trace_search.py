"""Trace 原记录的搜索与临时定位；位置独立于 Session 资源 URI。"""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId
from app.schemas.internal_v2.trace import TraceEventDTO

TraceSearchLimit = Literal["result_limit", "scan_limit", "byte_limit", "deadline", "item_size_limit"]


class TraceReferenceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: CanonicalSessionId
    thread_id: CanonicalThreadId
    event_id: str = Field(min_length=1)
    line_start: int = Field(ge=0)
    line_end: int = Field(gt=0)
    content_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_codepoint: int = Field(ge=0)
    end_codepoint: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.line_end <= self.line_start or self.end_codepoint <= self.start_codepoint:
            raise ValueError("Trace 行及正文位置必须是非空半开区间")
        return self


class TraceMatchDTO(BaseModel):
    reference: TraceReferenceDTO
    event_type: str
    timestamp: datetime
    preview: str
    preview_start_codepoint: int = Field(ge=0)


class TraceLocationDTO(BaseModel):
    reference: TraceReferenceDTO
    record: TraceEventDTO
    text: str = Field(description="原事件保留字段顺序、两空格缩进的 Unicode JSON；位置采用码点")


class WorkspaceTraceMatchDTO(BaseModel):
    session_uri: str
    session_title: str
    match: TraceMatchDTO


class WorkspaceTraceSearchResultsDTO(BaseModel):
    catalog_revision: int
    items: list[WorkspaceTraceMatchDTO]
    cursor: str | None = None
    complete: bool
    limited_reason: TraceSearchLimit | None = None
    scanned_nodes: int = Field(ge=0)
    scanned_records: int = Field(ge=0)
    scanned_bytes: int = Field(ge=0)
