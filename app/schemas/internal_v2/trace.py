from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TraceEventDTO(BaseModel):
    event_id: str
    part_id: str | None = None
    session_id: str
    job_id: str
    type: Literal[
        "agent_start",
        "llm_request",
        "model_failed",
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
        "message_created",
        "session_interrupted",
        "goal_updated",
        "goal_cleared",
    ]
    phase: Literal["agent", "llm", "tool", "error", "job", "text", "system", "status", "message", "session", "goal"]
    title: str
    content: str
    status: str | None = None
    tool_name: str | None = None
    skill_names: list[str] = Field(default_factory=list)
    step_id: str | None = None
    timestamp: datetime
    raw: dict[str, Any] = Field(default_factory=dict)
