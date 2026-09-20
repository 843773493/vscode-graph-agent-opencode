"""checkpoint/runtime reminder 的 source lifecycle producer。

提醒是 runtime source，不是 LangGraph message checkpoint。该模块只构造稳定的
``ApplySourceLifecycleDecision``，由唯一 Saver/ContextStore owner 写入
``runtime_notice`` pending item；本模块不读取或改写 checkpoint。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime

from app.domain.itemized.mutation_intents import (
    ApplySourceLifecycleDecision,
    MutationIntentOwner,
)
from app.prompting import internal_message_factory
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    MAIN_THREAD_ID,
)


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


def _revision(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _event_identity(
    *,
    session_id: str,
    checkpoint_source: str,
    response_metadata: Mapping[str, object],
    explicit: str | None,
) -> str:
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit:
            raise ValueError("checkpoint reminder event_identity 必须是非空字符串")
        return explicit
    for key in (
        "checkpoint_event_id",
        "execution_id",
        "turn_id",
        "interrupt_request_id",
        "job_id",
        "tool_invocation_id",
    ):
        value = response_metadata.get(key)
        if isinstance(value, str) and value:
            return value
    # 调用方没有提供执行身份时只能把 session+reason 作为稳定边界；重复事件
    # 将按 item identity 幂等复用，不从当前 checkpoint 内容猜测新 ID。
    return f"{session_id}:{checkpoint_source}"


def submit_checkpoint_reminder(
    *,
    checkpointer: object,
    session_id: str,
    reminder: str,
    response_metadata: Mapping[str, object],
    checkpoint_source: str,
    event_identity: str | None = None,
) -> bool:
    """向唯一 source intent owner 提交一次 pending runtime reminder。"""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("checkpoint reminder session_id 必须是非空字符串")
    if not isinstance(reminder, str):
        raise TypeError("checkpoint reminder reminder 必须是字符串")
    if not isinstance(response_metadata, Mapping):
        raise TypeError("checkpoint reminder response_metadata 必须是 object")
    if not isinstance(checkpoint_source, str) or not checkpoint_source:
        raise ValueError("checkpoint reminder checkpoint_source 必须是非空字符串")

    prepared = internal_message_factory.build(
        kind="checkpoint_reminder",
        control=reminder,
        metadata={
            **dict(response_metadata),
            "checkpoint_source": checkpoint_source,
        },
    )
    identity = _event_identity(
        session_id=session_id,
        checkpoint_source=checkpoint_source,
        response_metadata=response_metadata,
        explicit=event_identity,
    )
    source_id = f"checkpoint:{checkpoint_source}:{identity}"
    revision = _revision(prepared.content)
    decision = ApplySourceLifecycleDecision(
        owner=MutationIntentOwner(
            session_id=session_id,
            thread_id=MAIN_THREAD_ID,
        ),
        source_id=source_id,
        source_kind="checkpoint_reminder",
        name=checkpoint_source,
        decision_kind="observe_pending",
        revision=revision,
        pending_only=True,
        content=prepared.content,
        # reminder 的 item identity 绑定稳定事件，而不是正文 revision。这样
        # 同一事件重试时复用同一个 immutable item；若正文真的变化，owner 会
        # 以 identity/content 冲突显式拒绝，而不是悄悄追加第二条提醒。
        item_id=f"item-{source_id}",
        metadata=prepared.metadata,
    )
    consume = getattr(checkpointer, "consume_mutation_intent", None)
    if not callable(consume):
        raise TypeError(
            "checkpoint reminder owner 缺少 consume_mutation_intent 端口"
        )
    consume(decision)
    return True


def persist_interrupt_checkpoint(
    *,
    checkpointer: object | None,
    session_id: str,
    active_tool_name: str | None,
    checkpoint_source: str = "interrupt",
    event_identity: str | None = None,
) -> None:
    """任务被取消时提交 pending runtime reminder。

    半成品 assistant 文本由 stream/canonical producer 独立收敛；本函数只负责
    reminder source，避免把半成品 assistant 与 reminder 拼成第二份 checkpoint
    history。
    """
    if checkpointer is None:
        raise RuntimeError("任务取消时无法提交 runtime reminder：owner 未配置")

    phase = "tool" if active_tool_name else "text"
    interrupted_at = datetime.now(UTC).isoformat()
    if checkpoint_source == "interrupt":
        reminder = build_user_interrupt_reminder(
            phase=phase,
            active_tool_name=active_tool_name,
            interrupted_at=interrupted_at,
        )
    elif checkpoint_source == "job_timeout":
        reminder = (
            f"AgentLoop 在 {interrupted_at} 达到任务总超时上限并停止。"
            "请保留此前已完成的工具结果，根据最新用户请求继续或明确报告失败。"
        )
    else:
        reminder = (
            f"AgentLoop 在 {interrupted_at} 因内部执行丢失而停止，未收到用户中断请求。"
            "请保留此前已完成的工具结果，根据最新用户请求继续或明确报告失败。"
        )
    submit_checkpoint_reminder(
        checkpointer=checkpointer,
        session_id=session_id,
        reminder=reminder,
        response_metadata={
            "phase": phase,
            "tool_name": active_tool_name,
            "source": checkpoint_source,
        },
        checkpoint_source=checkpoint_source,
        event_identity=event_identity,
    )


__all__ = [
    "build_user_interrupt_reminder",
    "persist_interrupt_checkpoint",
    "submit_checkpoint_reminder",
]
