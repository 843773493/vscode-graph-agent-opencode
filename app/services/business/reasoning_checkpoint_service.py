from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.agents.providers.response_normalization import canonicalize_ai_message
from app.core.checkpoint_config import build_checkpoint_config
from app.schemas.event import ModelTokenUsagePayload

_STALE_EXECUTION_CHANNEL = "__pregel_tasks"


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


def _content_part_refs(
    content_blocks: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """保存流式 part 的稳定引用，避免 provider canonicalizer 丢失临时字段。"""
    refs: list[dict[str, object]] = []
    for index, block in enumerate(content_blocks):
        part_id = block.get("id")
        part_index = block.get("index", index)
        if not isinstance(part_id, str) or not part_id:
            continue
        if not isinstance(part_index, int) or isinstance(part_index, bool):
            raise TypeError("assistant content part index 必须是整数")
        refs.append(
            {
                "id": part_id,
                "index": part_index,
                "type": block.get("type"),
            }
        )
    return refs


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
    turn_id: str | None,
    preserve_content_part_refs: bool,
) -> AIMessage | None:
    latest_index = _latest_final_assistant_index(messages)
    if latest_index < 0:
        return None
    latest = messages[latest_index]
    if not isinstance(latest, AIMessage):
        raise TypeError("最终 assistant 定位结果不是 AIMessage")

    response_metadata = dict(latest.response_metadata or {})
    response_metadata["phase"] = "final_answer"
    response_metadata["message_id"] = message_id
    response_metadata["created_at"] = message_created_at.isoformat()
    response_metadata["updated_at"] = message_created_at.isoformat()
    superseded_message_id = latest.id or latest.response_metadata.get("message_id")
    if (
        isinstance(superseded_message_id, str)
        and superseded_message_id
        and superseded_message_id != message_id
    ):
        # canonical item 不可覆盖；最终收敛消息显式指向它替代的 LangGraph
        # carrier，供下次 Provider context 按 identity 排除旧副本。
        response_metadata["supersedes_message_id"] = superseded_message_id
    if turn_id is not None:
        raw_message_metadata = response_metadata.get("message_metadata")
        message_metadata = (
            dict(raw_message_metadata)
            if isinstance(raw_message_metadata, Mapping)
            else {}
        )
        # compaction/runtime 的内部消息可能成为当前 checkpoint 中的
        # latest AIMessage。最终可见 assistant 必须重新绑定到本次真实
        # Turn，不能继承 internal-* 的临时归属，否则 finalization 会把
        # 已写入的消息从目标 Turn 中排除。
        message_metadata.pop("internal", None)
        message_metadata["turn_id"] = turn_id
        message_metadata["job_id"] = turn_id
        response_metadata["message_metadata"] = message_metadata
    if token_usage is not None and token_usage.reported_model_calls > 0:
        response_metadata["token_usage"] = token_usage.model_dump(mode="json")
    if preserve_content_part_refs:
        refs = _content_part_refs(content_blocks)
        if refs:
            response_metadata["content_part_refs"] = refs
    rewritten = latest.model_copy(
        update={
            "id": message_id,
            "content": _build_assistant_content(content_blocks, final_text),
            "additional_kwargs": {},
            "response_metadata": response_metadata,
        }
    )
    messages.append(rewritten)
    return rewritten


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
    preserve_content_part_refs: bool = False,
    before_persist: Callable[[AIMessage], None] | None = None,
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
    rewritten = _rewrite_latest_assistant_message(
        messages,
        content_blocks=content_blocks,
        final_text=final_text,
        message_id=message_id,
        message_created_at=message_created_at,
        token_usage=token_usage,
        turn_id=turn_id,
        preserve_content_part_refs=preserve_content_part_refs,
    )
    if rewritten is None:
        return False

    if before_persist is not None:
        before_persist(rewritten)

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
    itemized_convergence = callable(
        getattr(checkpointer, "converge_execution", None)
    ) and callable(getattr(checkpointer, "append_items", None))
    if turn_id is not None and not itemized_convergence:
        if not callable(finalize_turn):
            raise RuntimeError("当前 checkpoint saver 不支持 Turn finalization")
        finalize_turn(
            session_id=session_id,
            turn_id=turn_id,
            final_message_id=message_id,
        )
    return True


def persist_intermediate_assistant_reasoning_checkpoint(
    *,
    checkpointer: BaseCheckpointSaver,
    session_id: str,
    model_content_blocks: Sequence[Sequence[Mapping[str, object]]],
) -> bool:
    """把带工具调用的中间模型 reasoning 补入 checkpoint 兼容视图。

    事件流 sink 已将同一份 reasoning 写入 canonical item。这里仅更新
    LangGraph 的兼容 checkpoint，使 Agent State 能观察到模型在工具分派前的
    reasoning；不创建 item，也不把兼容消息反向作为 canonical 来源。
    """
    if not model_content_blocks:
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

    tool_message_indexes = [
        index
        for index, message in enumerate(raw_messages)
        if isinstance(message, AIMessage) and bool(getattr(message, "tool_calls", None))
    ]
    changed = False
    messages = list(raw_messages)
    for message_index, blocks in zip(tool_message_indexes, model_content_blocks):
        reasoning_blocks = [
            dict(block)
            for block in blocks
            if isinstance(block, Mapping)
            and block.get("type")
            in {
                "reasoning",
                "reasoning_content",
                "reasoning_items",
                "thinking",
                "redacted_thinking",
            }
        ]
        if not reasoning_blocks:
            continue
        message = messages[message_index]
        if not isinstance(message, AIMessage):
            continue
        existing_content = message.content
        existing_blocks = (
            [dict(block) for block in existing_content if isinstance(block, Mapping)]
            if isinstance(existing_content, list)
            else []
        )
        if any(
            block.get("type")
            in {
                "reasoning",
                "reasoning_content",
                "reasoning_items",
                "thinking",
                "redacted_thinking",
            }
            for block in existing_blocks
        ):
            # LangGraph 可能已经把带 reasoning 的 tool-call message 写入
            # checkpoint。此时只补同一内容的 metadata 会让 canonical group
            # 重新编码并改变成员/语义校验；canonical stream 已经是权威来源，
            # 直接保持该兼容消息不变即可。
            continue
        existing_keys = {
            (
                block.get("id"),
                block.get("type"),
                block.get("index"),
            )
            for block in existing_blocks
        }
        merged_blocks = [
            *existing_blocks,
            *[
                block
                for block in reasoning_blocks
                if (block.get("id"), block.get("type"), block.get("index"))
                not in existing_keys
            ],
        ]
        if merged_blocks == existing_blocks:
            continue
        response_metadata = dict(message.response_metadata or {})
        response_metadata["reasoning_source"] = "canonical_item_stream"
        response_metadata["phase"] = "commentary"
        messages[message_index] = message.model_copy(
            update={
                "content": merged_blocks,
                "response_metadata": response_metadata,
            }
        )
        changed = True

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
        metadata={"source": "canonical_item_reasoning_projection", "step": -1, "writes": {}},
        new_versions={"messages": messages_version},
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

    # RolloutCheckpointSaver 在 acceptance-time 先建立 root/Turn/execution，
    # 随后的 LangGraph checkpoint 只负责把同一个 root 投影回 messages channel。
    # 旧 saver 没有该 owner API 时继续走其只读兼容路径。
    accept_turn = getattr(checkpointer, "accept_turn", None)
    if callable(accept_turn):
        message_metadata = response_metadata.get("message_metadata")
        turn_id = (
            message_metadata.get("turn_id")
            if isinstance(message_metadata, Mapping)
            else None
        )
        if not isinstance(turn_id, str) or not turn_id:
            # 旧调用方没有把 job id 放入 message_metadata 时，使用稳定的
            # ingress-derived id；这仍然在 acceptance-time 创建真实 Turn，
            # 不从 wire role 或物理邻接猜测。
            turn_id = f"turn-{message_id}"
        accepted = accept_turn(
            session_id,
            accepted_ingress_id=message_id,
            acceptance_idempotency_key=f"message:{message_id}",
            payload=message.content,
            payload_kind=(
                "text" if isinstance(message.content, str) else "structured_content"
            ),
            turn_id=turn_id,
            root_item_id=f"item-{message_id}",
            acceptance_metadata={
                "message_created_at": response_metadata.get("created_at"),
                # acceptance-time canonical root 必须保留消息的业务可见性。
                # 否则内部 Goal/调度消息会在 LangChain projection 往返后
                # 丢失 internal 标记，并被历史 API 当作真实用户输入展示。
                "message_metadata": dict(message_metadata or {}),
            },
        )
        if not isinstance(accepted, Mapping):
            raise TypeError("RolloutCheckpointSaver.accept_turn 返回值非法")

    config = build_checkpoint_config(session_id)
    tup = checkpointer.get_tuple(config)
    if tup is None:
        # acceptance 已经先于 provider dispatch 固化；旧 LangGraph saver
        # 可能尚未建立 tuple，调用方仍可继续执行并由后续 checkpoint 建立它。
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
    # 失败/重启后的 LangGraph checkpoint 可能仍保留上一轮已经失效的
    # Pregel task。新用户消息只能从干净的会话上下文启动，不能把旧工具
    # Send 重新交给当前 AgentLoop；否则新 job 会显示 running，却永远等
    # 不到自己的首个模型/工具事件。
    channel_values.pop(_STALE_EXECUTION_CHANNEL, None)
    checkpoint["pending_sends"] = []
    checkpoint["channel_values"] = channel_values
    checkpoint["id"] = str(uuid.uuid4())

    channel_versions = dict(checkpoint.get("channel_versions", {}))
    channel_versions.pop(_STALE_EXECUTION_CHANNEL, None)
    messages_version = checkpointer.get_next_version(
        channel_versions.get("messages"), None
    )
    channel_versions["messages"] = messages_version
    checkpoint["channel_versions"] = channel_versions
    updated_channels = [
        channel
        for channel in checkpoint.get("updated_channels", [])
        if channel != _STALE_EXECUTION_CHANNEL
    ]
    if "messages" not in updated_channels:
        updated_channels.append("messages")
    checkpoint["updated_channels"] = updated_channels
    checkpointer.put(
        config=tup.config,
        checkpoint=checkpoint,
        metadata={"source": "user_message_checkpoint", "step": -1, "writes": {}},
        new_versions={"messages": messages_version},
    )
    return True
