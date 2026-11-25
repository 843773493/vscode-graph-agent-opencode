"""Agent 执行输入与最终回复的 LangGraph 检查点适配。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    ExecutionBinding,
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.services.infrastructure.rollout_context.checkpoint.codec.message_channel import (
    checkpoint_messages,
    message_snapshot,
    message_snapshot_metadata,
)

_STALE_EXECUTION_CHANNEL = "__pregel_tasks"


def finalize_assistant_checkpoint(
    *, checkpointer: BaseCheckpointSaver, thread_id: str, message_id: str | None,
    on_finalize: Callable[[AIMessage], None],
) -> None:
    """按最终模型事件的原消息身份读取，不追加替代消息或重写 checkpoint。"""
    if not message_id:
        raise ValueError("最终模型事件缺少原 message_id")
    checkpoint = checkpointer.get_tuple(build_checkpoint_config(thread_id))
    if checkpoint is None:
        raise RuntimeError("最终模型输出尚无已提交 checkpoint")
    messages = checkpoint_messages(checkpointer, checkpoint)
    matches = [message for message in messages if message.id == message_id]
    if len(matches) != 1 or not isinstance(matches[0], AIMessage) or matches[0].tool_calls:
        raise ValueError(f"最终模型事件未对应唯一的 assistant 输出消息: {message_id}")
    on_finalize(matches[0])


@dataclass(frozen=True, slots=True)
class AcceptedExecutionInput:
    """当前输入消费操作取得的权威准入、原消息与 checkpoint 写入回执。"""

    admission: ExecutionAdmission
    message: HumanMessage
    checkpointed: bool


def checkpoint_accepted_execution_input(
    *,
    checkpointer: BaseCheckpointSaver,
    session_id: str,
    thread_id: str,
    job_id: str,
    message_id: str,
    execution_binding: ExecutionBinding,
    checkpoint_ns: str = "",
    write_checkpoint: bool = True,
) -> AcceptedExecutionInput:
    """一次读取并消费已接受输入；恢复原 checkpoint 时只取得同一权威输入。"""
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("执行输入缺少持久化 message_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("执行输入 checkpoint 缺少真实 thread_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("执行输入 checkpoint 缺少真实 job_id")
    if not isinstance(execution_binding, (UserExecutionIdentity, InternalExecutionIntent)):
        raise TypeError("执行输入必须携带 typed execution binding")
    execution_intent = execution_binding if isinstance(execution_binding, InternalExecutionIntent) else None
    if execution_intent is not None and execution_intent.message_id != message_id:
        raise ValueError("internal execution intent 与持久化消息 identity 不一致")
    if isinstance(execution_binding, UserExecutionIdentity) and execution_binding.job_id != job_id:
        raise ValueError("user execution identity 的 job_id 与当前 Job 不一致")

    read_message = getattr(checkpointer, "accepted_internal_message" if execution_intent is not None else "accepted_user_message", None)
    if not callable(read_message):
        raise TypeError("checkpoint saver 缺少 v2 execution admission owner 端口")
    options = {"intent": execution_intent} if execution_intent is not None else {}
    admission, message = read_message(
        session_id, thread_id=thread_id, job_id=job_id, message_id=message_id,
        checkpoint_ns=checkpoint_ns, **options,
    )
    expected_origin = "internal" if execution_intent is not None else "user_turn"
    if not isinstance(admission, ExecutionAdmission):
        raise TypeError("checkpoint saver 返回了非法 execution admission")
    if (
        admission.session_id != session_id
        or admission.thread_id != thread_id
        or admission.job_id != job_id
        or admission.admission_idempotency_key != execution_binding.admission_idempotency_key
        or admission.execution_origin != expected_origin
        or (execution_intent is not None and (admission.turn_id is not None or admission.execution_id != execution_intent.execution_id))
        or (isinstance(execution_binding, UserExecutionIdentity) and admission.turn_id != execution_binding.turn_id)
    ):
        raise RuntimeError(
            "message checkpoint admission 与 SessionThread/job/origin 不一致: "
            f"session_id={session_id}, thread_id={thread_id}, job_id={job_id}"
        )
    if not isinstance(message, HumanMessage) or message.response_metadata.get("message_id") != message_id:
        raise ValueError("执行输入 reader 未返回原消息 identity")
    if not write_checkpoint:
        return AcceptedExecutionInput(admission, message, False)

    config = build_checkpoint_config(thread_id)
    with execution_admission_scope(admission, langgraph_thread_id=thread_id):
        tup = checkpointer.get_tuple(config)
    if tup is None:
        # 首次执行尚无 LangGraph tuple，后续图执行会建立 checkpoint。
        return AcceptedExecutionInput(admission, message, False)

    checkpoint = tup.checkpoint.copy()
    channel_values = dict(checkpoint.get("channel_values", {}))
    raw_messages = checkpoint_messages(checkpointer, tup)
    if not isinstance(raw_messages, list):
        raise TypeError(
            f"LangGraph checkpoint messages 应为 list，实际类型: {type(raw_messages).__name__}"
        )

    append_user_input = execution_intent is None and not any(
        isinstance(existing, HumanMessage)
        and existing.response_metadata.get("message_id") == message_id
        for existing in raw_messages
    )
    input_uncheckpointed = False
    if execution_intent is None:
        frozen_tuple = checkpointer.get_tuple(tup.config)
        if frozen_tuple is None:
            raise RuntimeError("已解析的冻结 checkpoint 在执行期间消失")
        frozen_messages = checkpoint_messages(checkpointer, frozen_tuple)
        input_uncheckpointed = not any(
            isinstance(existing, HumanMessage)
            and existing.response_metadata.get("message_id") == message_id
            for existing in frozen_messages
        )
    stale_execution_state = (
        _STALE_EXECUTION_CHANNEL in channel_values
        or _STALE_EXECUTION_CHANNEL in checkpoint.get("channel_versions", {})
        or _STALE_EXECUTION_CHANNEL in checkpoint.get("updated_channels", [])
        or bool(checkpoint.get("pending_sends"))
    )
    # 即时 root 可见性不代表 LangGraph channel 已消费它；只在活动执行中
    # 固化 channel，且不重复追加同 ID 的输入。
    if not (append_user_input or input_uncheckpointed or stale_execution_state):
        return AcceptedExecutionInput(admission, message, False)
    channel_values["messages"] = message_snapshot([*raw_messages, message] if append_user_input else list(raw_messages))
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
            metadata={**message_snapshot_metadata(tup.metadata), "source": "user_message_checkpoint", "step": -1, "writes": {}},
            new_versions={"messages": messages_version},
        )
    return AcceptedExecutionInput(admission, message, True)
