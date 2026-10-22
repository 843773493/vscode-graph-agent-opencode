from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.canonical_params import CanonicalThreadId


class TraceEventDTO(BaseModel):
    event_id: str
    part_id: str | None = Field(default=None, description="来源路径提供的部件引用；历史工具路径在已知 emitting ModelCall 时使用其限定的聚合引用，通用 Trace 不承诺所有 part_id 都遵循该格式。")
    session_id: str
    thread_id: CanonicalThreadId = Field(description="事件实际所属的 Thread；物理日志仍由 Session 持有。")
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
    timestamp: datetime = Field(description="来源事件的带时区时间，Trace 映射保留其时刻与显式时区偏移，不在投影时重新取时；历史工具事件沿对应 canonical 消息/item 时间。")
    raw: dict[str, Any] = Field(default_factory=dict)
