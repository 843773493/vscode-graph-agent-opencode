from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.agents.providers.response_normalization import canonicalize_ai_message
from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    InternalExecutionIntent,
)
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
        message_metadata.pop("job_id", None)
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
    thread_id: str,
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
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("最终 assistant checkpoint 缺少真实 thread_id")
    if not content_blocks and not final_text:
        return False

    config = build_checkpoint_config(thread_id)
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
            thread_id=thread_id,
            turn_id=turn_id,
            final_message_id=message_id,
        )
    return True


def persist_user_message_checkpoint(
    *,
    checkpointer: BaseCheckpointSaver,
    session_id: str,
    thread_id: str,
    job_id: str,
    admission_idempotency_key: str,
    message: HumanMessage,
    execution_intent: InternalExecutionIntent | None = None,
    checkpoint_ns: str = "",
) -> bool:
    """在模型执行前固化真实 Turn acceptance 或 Turn-less internal admission。"""
    response_metadata = message.response_metadata or {}
    message_id = response_metadata.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("用户消息缺少持久化 message_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("用户消息 checkpoint 缺少真实 thread_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("用户消息 checkpoint 缺少真实 job_id")
    if not isinstance(admission_idempotency_key, str) or not admission_idempotency_key:
        raise ValueError("用户消息 checkpoint 缺少独立 admission_idempotency_key")

    accept_turn = getattr(checkpointer, "accept_turn", None)
    admit_internal_execution = getattr(
        checkpointer, "admit_internal_execution", None
    )
    execution_for_turn = getattr(checkpointer, "execution_for_turn", None)
    read_admission = getattr(checkpointer, "execution_admission", None)
    if not all(
        callable(port)
        for port in (
            accept_turn,
            admit_internal_execution,
            execution_for_turn,
            read_admission,
        )
    ):
        raise TypeError("checkpoint saver 缺少 v2 execution admission owner 端口")

    if execution_intent is not None:
        if not isinstance(execution_intent, InternalExecutionIntent):
            raise TypeError("internal job execution_intent 类型非法")
        if execution_intent.message_id != message_id:
            raise ValueError(
                "internal execution intent 与持久化消息 identity 不一致: "
                f"intent={execution_intent.message_id!r}, message={message_id!r}"
            )
        if execution_intent.admission_idempotency_key != admission_idempotency_key:
            raise ValueError(
                "internal execution intent 与 Job admission idempotency key 不一致"
            )
        admission = admit_internal_execution(
            session_id,
            thread_id=thread_id,
            job_id=job_id,
            intent=execution_intent,
            message=message,
            checkpoint_ns=checkpoint_ns,
        )
    else:
        message_metadata = response_metadata.get("message_metadata")
        turn_id = (
            message_metadata.get("turn_id")
            if isinstance(message_metadata, Mapping)
            else None
        )
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("user_turn checkpoint 缺少显式真实 turn_id")
        accepted = accept_turn(
            session_id,
            thread_id=thread_id,
            accepted_ingress_id=message_id,
            acceptance_idempotency_key=f"message:{message_id}",
            job_id=job_id,
            admission_idempotency_key=admission_idempotency_key,
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
            checkpoint_ns=checkpoint_ns,
        )
        if not isinstance(accepted, Mapping):
            raise TypeError("RolloutCheckpointSaver.accept_turn 返回值非法")
        execution_id = accepted.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise RuntimeError("Turn acceptance 未返回 initial execution identity")
        admission = read_admission(
            session_id,
            thread_id=thread_id,
            execution_id=execution_id,
            checkpoint_ns=checkpoint_ns,
        )
        if not isinstance(admission, ExecutionAdmission):
            raise TypeError("RolloutCheckpointSaver.execution_admission 返回值非法")
        if admission.turn_id != turn_id:
            raise RuntimeError(
                "Turn acceptance 与 initial execution admission 的 turn_id 不一致: "
                f"accepted={turn_id!r}, admission={admission.turn_id!r}"
            )
    expected_origin = "internal" if execution_intent is not None else "user_turn"
    if not isinstance(admission, ExecutionAdmission):
        raise TypeError("checkpoint saver 返回了非法 execution admission")
    if (
        admission.session_id != session_id
        or admission.thread_id != thread_id
        or admission.job_id != job_id
        or admission.admission_idempotency_key != admission_idempotency_key
        or admission.execution_origin != expected_origin
        or (expected_origin == "internal" and admission.turn_id is not None)
        or (expected_origin == "user_turn" and admission.turn_id is None)
    ):
        raise RuntimeError(
            "message checkpoint admission 与 SessionThread/job/origin 不一致: "
            f"session_id={session_id}, thread_id={thread_id}, job_id={job_id}"
        )

    config = build_checkpoint_config(thread_id)
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
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        checkpointer.put(
            config=tup.config,
            checkpoint=checkpoint,
            metadata={"source": "user_message_checkpoint", "step": -1, "writes": {}},
            new_versions={"messages": messages_version},
        )
    return True
