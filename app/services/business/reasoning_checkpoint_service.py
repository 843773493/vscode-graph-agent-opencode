from __future__ import annotations

import uuid

from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core.checkpoint_config import build_checkpoint_config
from app.schemas.event import ModelTokenUsagePayload


def _build_assistant_content(
    reasoning_text: str,
    final_text: str,
) -> list[dict[str, str]]:
    content: list[dict[str, str]] = []
    if reasoning_text:
        content.append({"type": "reasoning", "reasoning": reasoning_text})
    if final_text:
        content.append({"type": "text", "text": final_text})
    return content


def _latest_final_assistant_index(messages: list[object]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, AIMessage):
            continue
        if getattr(message, "tool_calls", None):
            continue
        return index
    return -1


def _rewrite_latest_assistant_message(
    messages: list[object],
    *,
    reasoning_text: str,
    final_text: str,
    token_usage: ModelTokenUsagePayload | None,
) -> bool:
    index = _latest_final_assistant_index(messages)
    if index < 0:
        return False

    message = messages[index]
    response_metadata = dict(message.response_metadata or {})
    response_metadata["phase"] = "final_answer"
    if token_usage is not None and token_usage.reported_model_calls > 0:
        response_metadata["token_usage"] = token_usage.model_dump(mode="json")
    messages[index] = message.model_copy(
        update={
            "content": _build_assistant_content(reasoning_text, final_text),
            "additional_kwargs": {},
            "response_metadata": response_metadata,
        }
    )
    return True


def persist_standard_assistant_checkpoint(
    *,
    checkpointer: BaseCheckpointSaver,
    session_id: str,
    reasoning_text: str,
    final_text: str,
    token_usage: ModelTokenUsagePayload | None = None,
) -> bool:
    """把本轮最终 assistant 消息保存为 LangChain 标准 content blocks。"""
    if not reasoning_text and not final_text:
        return False

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

    messages = list(raw_messages)
    changed = _rewrite_latest_assistant_message(
        messages,
        reasoning_text=reasoning_text,
        final_text=final_text,
        token_usage=token_usage,
    )
    if not changed:
        return False

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
        metadata={"source": "standard_assistant_content", "step": -1, "writes": {}},
        new_versions={"messages": messages_version},
    )
    return True
