from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core.checkpoint_config import build_checkpoint_config


def build_user_interrupt_reminder(
    *,
    phase: str,
    active_tool_name: str | None,
    interrupted_at: datetime | str,
) -> str:
    if isinstance(interrupted_at, datetime):
        interrupted_at_text = interrupted_at.isoformat()
    else:
        interrupted_at_text = interrupted_at

    if phase == "tool" and active_tool_name:
        return (
            f"用户在你调用工具（{active_tool_name}）的过程中于 {interrupted_at_text} 主动取消。"
            "当前工具调用已被取消，请停止当前操作，根据已有信息回应用户最新请求。"
        )
    return (
        f"用户在文本生成过程中于 {interrupted_at_text} 主动取消。"
        "请停止当前输出，根据已有信息回应用户最新请求。"
    )


def _message_has_content(message: object) -> bool:
    content = getattr(message, "content", None)
    if content is None:
        return False
    if isinstance(content, list):
        return any(bool(part) for part in content)
    return bool(str(content).strip())


def append_system_reminder_checkpoint(
    *,
    checkpointer: BaseCheckpointSaver,
    session_id: str,
    reminder: str,
    response_metadata: dict[str, Any],
    assistant_text: str = "",
    assistant_response_metadata: dict[str, Any] | None = None,
    checkpoint_source: str = "system_reminder",
) -> bool:
    config = build_checkpoint_config(session_id)
    tup = checkpointer.get_tuple(config)
    if tup is None:
        return False

    checkpoint = tup.checkpoint.copy()
    channel_values = dict(checkpoint.get("channel_values", {}))
    raw_messages = channel_values.get("messages", [])
    if not isinstance(raw_messages, list):
        raise TypeError(
            f"LangGraph checkpoint messages 应为 list，实际类型: {type(raw_messages).__name__}"
        )

    messages = [
        msg for msg in raw_messages
        if not (isinstance(msg, AIMessage) and not _message_has_content(msg))
    ]

    if assistant_text.strip():
        messages.append(
            AIMessage(
                content=assistant_text,
                tool_calls=[],
                response_metadata=assistant_response_metadata or {},
            )
        )

    messages.append(
        HumanMessage(
            content=f"<system_reminder>\n{reminder}\n</system_reminder>",
            response_metadata=response_metadata,
        )
    )

    channel_values["messages"] = messages
    checkpoint["channel_values"] = channel_values
    checkpoint["id"] = str(uuid.uuid4())

    channel_versions = dict(checkpoint.get("channel_versions", {}))
    messages_version = checkpointer.get_next_version(
        channel_versions.get("messages"), None
    )
    channel_versions["messages"] = messages_version
    checkpoint["channel_versions"] = channel_versions

    checkpointer.put(
        config=tup.config,
        checkpoint=checkpoint,
        metadata={"source": checkpoint_source, "step": -1, "writes": {}},
        new_versions={"messages": messages_version},
    )
    return True


def persist_interrupt_checkpoint(
    *,
    checkpointer: BaseCheckpointSaver | None,
    session_id: str,
    current_text: str,
    active_tool_name: str | None,
) -> None:
    """任务被取消时，把部分 assistant 文本和独立 system_reminder 写入 checkpoint。"""
    if checkpointer is None:
        raise RuntimeError("任务取消时无法写入 checkpoint：checkpointer 未配置")

    phase = "tool" if active_tool_name else "text"
    interrupted_at = datetime.now(timezone.utc).isoformat()
    reminder = build_user_interrupt_reminder(
        phase=phase,
        active_tool_name=active_tool_name,
        interrupted_at=interrupted_at,
    )
    content = current_text if phase == "text" else ""
    injected = append_system_reminder_checkpoint(
        checkpointer=checkpointer,
        session_id=session_id,
        reminder=reminder,
        response_metadata={
            "phase": phase,
            "tool_name": active_tool_name,
            "source": "interrupt",
        },
        assistant_text=content,
        assistant_response_metadata={
            "phase": phase,
            "tool_name": active_tool_name,
            "source": "interrupt",
        },
        checkpoint_source="interrupt",
    )
    if not injected:
        raise RuntimeError(f"任务取消时未找到可写入的 checkpoint: session_id={session_id}")
