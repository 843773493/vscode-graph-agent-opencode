from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.agents.custom_tools import CustomToolFactoryContext


class ReadSessionRecentTextMessagesInput(BaseModel):
    """读取另一个会话最近文本消息的参数。"""

    session_id: str = Field(description="要读取的目标会话 ID。")
    rounds: int = Field(
        default=5,
        ge=1,
        le=50,
        description="最近用户轮次数，默认 5。",
    )


def _content_text(content: object, *, assistant_text_blocks_only: bool) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            if not assistant_text_blocks_only:
                parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if assistant_text_blocks_only and item_type != "text":
            continue
        if not assistant_text_blocks_only and item_type not in {None, "text", "input_text"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _is_user_record(record: dict[str, object]) -> bool:
    role = record.get("role")
    message_type = record.get("type")
    text = _content_text(record.get("content"), assistant_text_blocks_only=False)
    return (
        (role == "user" or message_type == "human")
        and bool(text)
        and not text.strip().startswith("<system_reminder>")
    )


def _is_assistant_record(record: dict[str, object]) -> bool:
    return record.get("role") == "assistant" or record.get("type") == "ai"


def _select_recent_user_rounds_with_assistant_text(
    records: list[dict[str, object]],
    rounds: int,
) -> list[dict[str, str]]:
    user_indexes = [
        index for index, record in enumerate(records)
        if _is_user_record(record)
    ]
    if not user_indexes:
        return []

    start_index = user_indexes[-rounds] if len(user_indexes) > rounds else user_indexes[0]
    selected: list[dict[str, str]] = []
    for record in records[start_index:]:
        if _is_user_record(record):
            text = _content_text(record.get("content"), assistant_text_blocks_only=False)
            selected.append({"role": "user", "text": text})
            continue
        if not _is_assistant_record(record):
            continue
        if record.get("tool_calls"):
            continue
        text = _content_text(record.get("content"), assistant_text_blocks_only=True)
        if text:
            selected.append({"role": "assistant", "type": "text", "text": text})
    return selected


def create_read_session_recent_text_messages_tool(
    context: CustomToolFactoryContext,
) -> BaseTool:
    """创建读取另一个 session 最近用户轮次和模型文本消息的扩展工具。"""

    async def read_session_recent_text_messages(
        session_id: str,
        rounds: int = 5,
    ) -> str:
        target_session_id = session_id.strip()
        if not target_session_id:
            raise ValueError("session_id 不能为空")

        await context.session_service.get(target_session_id)
        records = await context.message_service.list_agent_state_records(
            target_session_id,
            strict=True,
        )
        messages = _select_recent_user_rounds_with_assistant_text(records, rounds)
        user_message_count = sum(1 for message in messages if message["role"] == "user")
        payload: dict[str, Any] = {
            "session_id": target_session_id,
            "rounds": rounds,
            "user_message_count": user_message_count,
            "messages": messages,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return StructuredTool.from_function(
        coroutine=read_session_recent_text_messages,
        name="read_session_recent_text_messages",
        description=(
            "读取另一个 session 的 Agent State messages，返回最近 N 轮用户消息，"
            "以及这些轮次之间的模型 text 消息。默认 N=5。"
        ),
        args_schema=ReadSessionRecentTextMessagesInput,
    )
