from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class TraceEventDTO(BaseModel):
    event_id: str
    session_id: str
    job_id: Optional[str] = None
    type: Literal["agent_start", "llm_request", "tool_call_start", "tool_call_end", "agent_end", "error"]
    phase: Literal["agent", "llm", "tool", "error"]
    title: str
    content: str
    status: Optional[str] = None
    tool_name: Optional[str] = None
    step_id: Optional[str] = None
    timestamp: datetime
    raw: dict[str, Any] = Field(default_factory=dict)
