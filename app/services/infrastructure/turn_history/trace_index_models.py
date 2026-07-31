from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TraceTurnIndexEntry(BaseModel):
    schema_version: Literal[1] = 1
    event_id: str
    event_type: str
    job_id: str
    projects_turn: bool = True
    trace_start: int = Field(ge=0)
    trace_end: int = Field(ge=1)
    message_start: int = Field(ge=0)
    message_end: int = Field(ge=1)
    # TODO: 索引 schema 升级并提供显式迁移后，移除旧 v1 的保守默认。
    source_compacted: bool = True
    compact_event: dict[str, object]


class TraceTurnIndexManifest(BaseModel):
    schema_version: Literal[1] = 1
    committed_index_offset: int = Field(default=0, ge=0)
    committed_trace_offset: int = Field(default=0, ge=0)
    committed_message_offset: int = Field(default=0, ge=0)
    event_cursor: str | None = None
    projected_message_offset: int = Field(default=0, ge=0)
    projected_trace_offset: int = Field(default=0, ge=0)
    latest_job_index_offset: int | None = Field(default=None, ge=0)
    latest_job_id: str | None = None
    has_unindexed_prefix: bool = False


class PreparedTraceTurnEntry(BaseModel):
    entry: TraceTurnIndexEntry
    index_start: int = Field(ge=0)
    index_end: int = Field(ge=1)
    previous_manifest: TraceTurnIndexManifest
