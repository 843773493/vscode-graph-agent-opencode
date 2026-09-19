from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

DebugMode = Literal["ai", "human", "collaborative"]
DebugBreakpointKind = Literal["tool_before", "tool_after", "llm_before"]


class DebugBreakpointDTO(BaseModel):
    breakpoint_id: str
    kind: DebugBreakpointKind
    tool_name: str | None = None
    enabled: bool = True
    created_at: datetime


class DebugStopSnapshotDTO(BaseModel):
    stop_id: str
    job_id: str
    session_id: str
    point: DebugBreakpointKind
    reason: str
    tool_name: str | None = None
    args: dict[str, object] = Field(default_factory=dict)
    result: str | None = None
    breakpoint_id: str | None = None
    mode: DebugMode
    explanation: str
    stopped_at: datetime
    sequence: int = Field(ge=1)


class DebugActionRecordDTO(BaseModel):
    action_id: str
    job_id: str
    session_id: str
    action: str
    actor: Literal["human", "ai", "system"]
    mode: DebugMode
    message: str
    created_at: datetime


class AgentDebugStateDTO(BaseModel):
    job_id: str
    session_id: str
    enabled: bool
    mode: DebugMode
    paused: bool
    active_stop: DebugStopSnapshotDTO | None = None
    last_stop: DebugStopSnapshotDTO | None = None
    breakpoints: list[DebugBreakpointDTO] = Field(default_factory=list)
    actions: list[DebugActionRecordDTO] = Field(default_factory=list)
    stop_count: int = Field(default=0, ge=0)
