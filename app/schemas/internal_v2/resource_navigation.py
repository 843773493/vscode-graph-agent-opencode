"""与 workspace/v2/public.proto 一致的只读资源导航 HTTP 边界。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    max_bytes: int = Field(default=65536, ge=1, le=1048576)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    timeout_ms: int = Field(default=10000, ge=1, le=30000)
    scan_limit: int = Field(default=10000, ge=1, le=100000)
    max_response_bytes: int = Field(default=1048576, ge=1024, le=1048576)

    @model_validator(mode="after")
    def require_search_query(self) -> ResourceNavigationRequest:
        if self.operation == "search":
            if not self.query:
                raise ValueError("search 必须提供非空 query")
        elif self.query is not None:
            raise ValueError("只有 search 接受 query")
        if self.cursor is not None and self.operation not in {"list", "search"}:
            raise ValueError("只有 list/search 接受 cursor")
        return self


class ResourceNavigationNodeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    uri: str
    resource_shape: ResourceShape
    operations: tuple[ResourceNavigationOperation, ...]
    child_path: Literal["file_segments"] | None


class ResourceNavigationResultDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node: ResourceNavigationNodeDTO
    entries: tuple[ResourceNavigationNodeDTO, ...] = ()
    content: str | None = None
    truncated: bool = False
    next_cursor: str | None = None
    truncation_reason: Literal["entry_limit", "response_byte_limit", "file_byte_limit"] | None = None
    view: Literal["live"] = "live"
