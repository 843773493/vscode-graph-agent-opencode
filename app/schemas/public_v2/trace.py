from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class TraceEventDTO(BaseModel):
    event_id: str
    session_id: str
    job_id: Optional[str] = None
    type: Literal[
        "agent_start",
        "llm_request",
        "tool_call_start",
        "tool_call_end",
        "agent_end",
        "error",
        "job_created",
        "job_started",
        "job_completed",
        "job_cancelled",
        "job_failed",
        "status_change",
        "agent_step",
        "text_start",
        "text_delta",
        "text_end",
        "system_reminder_injected",
        "message_created",
        "session_interrupted",
    ]
    phase: Literal["agent", "llm", "tool", "error", "job", "text", "system", "status", "message", "session"]
    title: str
    content: str
    status: Optional[str] = None
    tool_name: Optional[str] = None
    step_id: Optional[str] = None
    timestamp: datetime
    raw: dict[str, Any] = Field(default_factory=dict)
