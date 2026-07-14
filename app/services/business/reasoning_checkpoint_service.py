from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core.checkpoint_config import build_checkpoint_config
from app.schemas.event import ModelTokenUsagePayload


def _build_assistant_content(
    content_blocks: Sequence[Mapping[str, object]],
    final_text: str,
) -> list[dict[str, object]]:
    content = [dict(block) for block in content_blocks]
    text_block_index = -1
    for index, block in enumerate(content):
        block_type = block.get("type")
        if block_type not in {"reasoning", "text", "refusal"}:
            raise ValueError(f"最终 assistant content 含未知 block type: {block_type!r}")
        part_id = block.get("id")
        if not isinstance(part_id, str) or not part_id:
            raise ValueError(f"最终 assistant content[{index}] 缺少 part id")
        block_index = block.get("index")
        if isinstance(block_index, bool) or not isinstance(block_index, int):
            raise ValueError(f"最终 assistant content[{index}] 缺少 block index")
        if block_type == "text":
            text_block_index = index

    if final_text:
        if text_block_index < 0:
            raise ValueError("最终 assistant 文本缺少对应的 text content block")
        content[text_block_index]["text"] = final_text
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
    content_blocks: Sequence[Mapping[str, object]],
    final_text: str,
    message_id: str,
    message_created_at: datetime,
    token_usage: ModelTokenUsagePayload | None,
) -> bool:
    index = _latest_final_assistant_index(messages)
    if index < 0:
        return False

    message = messages[index]
    response_metadata = dict(message.response_metadata or {})
    response_metadata["phase"] = "final_answer"
    response_metadata["message_id"] = message_id
    response_metadata["created_at"] = message_created_at.isoformat()
    response_metadata["updated_at"] = message_created_at.isoformat()
    if token_usage is not None and token_usage.reported_model_calls > 0:
        response_metadata["token_usage"] = token_usage.model_dump(mode="json")
    messages[index] = message.model_copy(
        update={
            "id": message_id,
            "content": _build_assistant_content(content_blocks, final_text),
            "additional_kwargs": {},
            "response_metadata": response_metadata,
        }
    )
    return True


def persist_standard_assistant_checkpoint(
    *,
    checkpointer: BaseCheckpointSaver,
    session_id: str,
    content_blocks: Sequence[Mapping[str, object]],
    final_text: str,
    message_id: str,
    message_created_at: datetime,
    token_usage: ModelTokenUsagePayload | None = None,
) -> bool:
    """把本轮最终 assistant 消息保存为 LangChain 标准 content blocks。"""
    if not message_id:
        raise ValueError("最终 assistant 消息缺少 message_id")
    if message_created_at.tzinfo is None:
        raise ValueError("最终 assistant message_created_at 必须包含时区")
    if not content_blocks and not final_text:
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
        content_blocks=content_blocks,
        final_text=final_text,
        message_id=message_id,
        message_created_at=message_created_at,
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
