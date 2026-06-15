"""System reminder 注入相关的 Schema 定义。"""
from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, Sequence

from pydantic import BaseModel, Field


class SystemReminderPosition(str, Enum):
    """system_reminder 在消息列表中的注入位置。"""

    AFTER_LAST_ASSISTANT = "after_last_assistant"
    AFTER_TOOL_CALLS = "after_tool_calls"
    AFTER_LAST_USER = "after_last_user"
    APPEND = "append"


class ToolResultSnapshot(BaseModel):
    """工具调用结果的快照，供触发器生成 reminder。"""

    tool_name: str
    result: str = ""
    interrupted: bool = False


class SystemReminder(BaseModel):
    """一条待注入的系统提醒。"""

    content: str
    position: SystemReminderPosition
    priority: int = 0
    dedup_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReminderTriggerContext(BaseModel):
    """触发器生成 reminder 时使用的上下文。"""

    session_id: str
    job_id: str
    agent_id: str
    messages: Sequence[Any] = Field(default_factory=list)
    last_turn_status: str = "ok"
    recent_tool_results: list[ToolResultSnapshot] = Field(default_factory=list)


class SystemReminderTrigger(Protocol):
    """system_reminder 触发器协议。"""

    def produce(self, ctx: ReminderTriggerContext) -> list[SystemReminder]:
        """根据上下文产生需要注入的 reminder 列表。"""
        ...
