"""历史消息查询与独立定位的业务约束；公开结构以 proto 为准。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.internal_v2.session_navigation.thread_search import (
    SessionThreadSearchResultDTO,
)


class TurnMessageSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=16384)
    category: Literal["message", "tool_result"] = "message"


class TurnMessageReferenceDTO(BaseModel):
    """canonical 正文中的零基 Unicode 码点半开区间。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=256)
    content_hash: str = Field(min_length=1, max_length=256)
    start_codepoint: int = Field(ge=0)
    end_codepoint: int = Field(ge=1)
    category: Literal["message", "tool_result"] = "message"

    @model_validator(mode="after")
    def validate_range(self) -> "TurnMessageReferenceDTO":
        if self.end_codepoint <= self.start_codepoint:
            raise ValueError("消息定位的结束位置必须大于起始位置")
        return self


class TurnMessageMatchDTO(BaseModel):
    turn_id: str
    message_id: str | None
    role: Literal["user", "assistant", "tool"]
    reference: TurnMessageReferenceDTO
    preview: str
    preview_start_codepoint: int = Field(ge=0)
    tool_call_id: str | None = None
    tool_name: str | None = None


class TurnMessageLocationDTO(BaseModel):
    """本次 around 请求核验的单条原文；不写入历史投影或前端历史缓存。"""

    turn_id: str
    message_id: str | None
    role: Literal["user", "assistant", "tool"]
    reference: TurnMessageReferenceDTO
    content: str
    tool_call_id: str | None = None
    tool_name: str | None = None


class TurnMessageSearchResultsDTO(BaseModel):
    session_id: str
    thread_id: str
    items: list[TurnMessageMatchDTO]
    cursor: str | None
    complete: bool
    limited_reason: Literal["result_limit", "scan_limit", "byte_limit", "deadline", "item_size_limit"] | None
    scanned_items: int = Field(ge=0)
    scanned_bytes: int = Field(ge=0)


class WorkspaceMessageMatchDTO(BaseModel):
    thread: SessionThreadSearchResultDTO
    match: TurnMessageMatchDTO


class WorkspaceMessageSearchResultsDTO(BaseModel):
    catalog_revision: int
    items: list[WorkspaceMessageMatchDTO]
    cursor: str | None
    complete: bool
    limited_reason: Literal["result_limit", "scan_limit", "thread_limit", "byte_limit", "deadline", "item_size_limit"] | None
    scanned_sessions: int = Field(ge=0)
    scanned_threads: int = Field(ge=0)
    scanned_items: int = Field(ge=0)
    scanned_bytes: int = Field(ge=0)


class TurnToolResultDTO(BaseModel):
    """原可见 canonical 结果及历史聚合调用身份，不含输出正文。"""

    item_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    status: str


class ThreadToolResultListDTO(BaseModel):
    session_id: str
    thread_id: str
    items: list[TurnToolResultDTO]
    next_after: int | None = None
    source_view_id: str | None
    source_view_revision: int
