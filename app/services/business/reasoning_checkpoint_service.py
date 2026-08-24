from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.agents.providers.litellm_content import canonicalize_ai_message
from app.core.checkpoint_config import build_checkpoint_config
from app.schemas.event import ModelTokenUsagePayload


def _build_assistant_content(
    content_blocks: Sequence[Mapping[str, object]],
    final_text: str,
) -> list[dict[str, object]]:
    # checkpoint 只接收已经收敛的直接 content block。流式阶段可能带有
    # part_*、index 和 extras，这里统一经过 provider 内容规范化器清理。
    canonical_message = canonicalize_ai_message(
        AIMessage(content=[dict(block) for block in content_blocks]),
        source_provider=None,
    )
    canonical_content = canonical_message.content
    if not isinstance(canonical_content, list):
        raise TypeError(
            "最终 assistant content 规范化后必须是 block list，"
            f"实际类型: {type(canonical_content).__name__}"
        )

    content = [
        dict(block)
        for block in canonical_content
        if isinstance(block, Mapping)
    ]
    text_block_index = -1
    for index, block in enumerate(content):
        block_type = block.get("type")
        if block_type not in {
            "reasoning",
            "reasoning_content",
            "reasoning_items",
            "text",
            "output_text",
            "refusal",
            "thinking",
            "redacted_thinking",
        }:
            raise ValueError(f"最终 assistant content 含未知 block type: {block_type!r}")
        if block_type in {"text", "output_text"}:
            text_block_index = index

    if final_text:
        if text_block_index < 0:
            raise ValueError("最终 assistant 文本缺少对应的 text content block")
        text_block = content[text_block_index]
        text_block["type"] = "text"
        text_block["text"] = final_text
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
    latest = next(
        (
            message
            for message in reversed(messages)
            if isinstance(message, AIMessage) and not message.tool_calls
        ),
        None,
    )
    if latest is None:
        return False

    response_metadata = dict(latest.response_metadata or {})
    response_metadata["phase"] = "final_answer"
    response_metadata["message_id"] = message_id
    response_metadata["created_at"] = message_created_at.isoformat()
    response_metadata["updated_at"] = message_created_at.isoformat()
    if token_usage is not None and token_usage.reported_model_calls > 0:
        response_metadata["token_usage"] = token_usage.model_dump(mode="json")
    messages.append(
        latest.model_copy(
            update={
                "id": message_id,
                "content": _build_assistant_content(content_blocks, final_text),
                "additional_kwargs": {},
                "response_metadata": response_metadata,
            }
        )
    )
    return True


def persist_standard_assistant_checkpoint(
    *,
    checkpointer: BaseCheckpointSaver,
    session_id: str,
    turn_id: str | None = None,
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
    finalize_turn = getattr(checkpointer, "finalize_turn", None)
    if turn_id is not None:
        if not callable(finalize_turn):
            raise RuntimeError("当前 checkpoint saver 不支持 Turn finalization")
        finalize_turn(
            session_id=session_id,
            turn_id=turn_id,
            final_message_id=message_id,
        )
    return True


def persist_user_message_checkpoint(
    *,
    checkpointer: BaseCheckpointSaver,
    session_id: str,
    message: HumanMessage,
) -> bool:
    """在模型执行前幂等固化用户消息，确保失败轮次仍可被重试。"""
    response_metadata = message.response_metadata or {}
    message_id = response_metadata.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("用户消息缺少持久化 message_id")

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

    for existing in raw_messages:
        if not isinstance(existing, HumanMessage):
            continue
        existing_metadata = existing.response_metadata or {}
        if existing_metadata.get("message_id") == message_id:
            return False

    messages = [*raw_messages, message]
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
        metadata={"source": "user_message_checkpoint", "step": -1, "writes": {}},
        new_versions={"messages": messages_version},
    )
    return True
