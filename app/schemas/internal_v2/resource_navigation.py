"""与 workspace/v2/public.proto 一致的只读资源导航 HTTP 边界。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.resource_uri import ResourceUriGrammarError, parse_resource_uri
from app.services.infrastructure.resource_platform.virtual_resources.values import (
    ResourceNavigationOperation,
    ResourceShape,
)


class ResourceNavigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str = Field(min_length=1)
    operation: ResourceNavigationOperation
    query: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    max_bytes: int = Field(default=65536, ge=1, le=33554432)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    timeout_ms: int = Field(default=10000, ge=1, le=30000)
    scan_limit: int = Field(default=10000, ge=1, le=100000)
    max_response_bytes: int = Field(default=1048576, ge=1024, le=50331648)
    read_format: Literal["text", "auto"] = "text"
    line_offset: int | None = Field(default=None, ge=1)
    max_lines: int | None = Field(default=None, ge=1)
    search_mode: Literal["path", "glob", "content"] = "path"
    file_glob: str | None = Field(default=None, min_length=1)
    max_snippet_bytes: int = Field(default=2048, ge=16, le=8192)

    @model_validator(mode="after")
    def require_search_query(self) -> ResourceNavigationRequest:
        if self.operation == "search":
            if not self.query:
                raise ValueError("search 必须提供非空 query")
        elif self.query is not None:
            raise ValueError("只有 search 接受 query")
        if self.cursor is not None and self.operation not in {"list", "search"}:
            raise ValueError("只有 list/search 接受 cursor")
        if self.operation != "read" and (self.read_format != "text" or self.line_offset is not None or self.max_lines is not None):
            raise ValueError("只有 read 接受格式和行范围")
        if self.operation != "search" and (self.search_mode != "path" or self.file_glob is not None):
            raise ValueError("只有 search 接受搜索模式和文件过滤")
        if self.file_glob is not None and self.search_mode != "content":
            raise ValueError("只有内容搜索接受 file_glob")
        return self


def _require_navigation_uri(value: str) -> str:
    try:
        parse_resource_uri(value)
    except ResourceUriGrammarError as error:
        # 输出合同故障只报告字段与原因；原生输入和文件正文不走此校验。
        raise ValueError(f"导航资源引用必须是资源 URI: code={error.reason_code}") from error
    return value


class ResourceNavigationNodeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True, hide_input_in_errors=True)

    uri: str
    resource_shape: ResourceShape
    operations: tuple[ResourceNavigationOperation, ...]
    child_path: Literal["file_segments"] | None = None
    _uri = field_validator("uri")(_require_navigation_uri)


class ResourceNavigationMatchDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    uri: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    text: str
    snippet_start_column: int = Field(ge=1)
    truncated: bool = False
    _uri = field_validator("uri")(_require_navigation_uri)


class ResourceNavigationResultDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    node: ResourceNavigationNodeDTO
    entries: tuple[ResourceNavigationNodeDTO, ...] = ()
    content: str | None = None
    truncated: bool = False
    next_cursor: str | None = None
    truncation_reason: Literal["entry_limit", "response_byte_limit", "file_byte_limit", "line_limit"] | None = None
    view: Literal["live"] = "live"
    content_encoding: Literal["utf-8", "base64"] | None = None
    media_type: Literal["text", "image", "audio", "video", "file"] | None = None
    mime_type: str | None = None
    line_offset: int | None = None
    matches: tuple[ResourceNavigationMatchDTO, ...] = ()
    content_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
