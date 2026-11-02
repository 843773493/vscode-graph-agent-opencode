"""工作区 Thread 名称发现；结果不是执行运行态或全局搜索对象。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.internal_v2.session_context import SessionContextURI


class SessionThreadSearchResultDTO(BaseModel):
    workspace_id: str
    session_id: str
    thread_id: str
    thread_uri: SessionContextURI
    kind: Literal["main", "child"]
    title: str
    session_title: str
    created_at: datetime


class SessionThreadSearchResultsDTO(BaseModel):
    catalog_revision: int
    items: list[SessionThreadSearchResultDTO] = Field(default_factory=list)
    cursor: str | None = None
    complete: bool
    limited_reason: Literal["result_limit", "scan_limit", "deadline"] | None = None
    scanned_sessions: int
    scanned_threads: int
