"""Agent execution 的 retry/reminder 规则。

本模块只生成控制文本和检查工具通信事实，不持有 canonical item、checkpoint
或 provider 状态；流程服务负责决定何时调用这些纯策略。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from langchain_core.messages import HumanMessage

from app.prompting import PromptSection, internal_message_factory
from app.services.orchestration.event_stream.contracts import (
    SuccessfulToolCall,
)


def cancelled_error_reason(error: asyncio.CancelledError) -> str | None:
    """从任务取消异常中恢复结构化 scope reason。"""
    message = str(error)
    scope_prefix = "运行时 scope 已取消: reason="
    if message.startswith(scope_prefix):
        return message.removeprefix(scope_prefix) or None
    return message or None


def build_empty_response_retry_reminder(attempt: int) -> str:
    return (
        "上一轮模型响应没有产生任何用户可见的最终回复，也没有完成可继续展示的结果。"
        "这通常表示你只输出了内部推理。"
        f"请继续处理当前用户请求，这是第 {attempt} 次空响应恢复。"
        "如果需要调用工具，必须通过工具调用通道真实调用工具；"
        "如果已经有足够信息，请输出用户可见的最终回复。"
        "不要只复述计划、步骤或内部思考。"
    )


def build_delegated_report_retry_reminder(
    *,
    parent_session_id: str,
    attempt: int,
    allow_progress: bool = False,
) -> str:
    progress_instruction = (
        "如果本轮收到的是下级进度，可以用 kind=progress 原样中继；"
        if allow_progress
        else ""
    )
    return (
        "这是委派子会话的首轮任务。你输出了普通最终文本，但父 Agent 不会自动收到它。"
        "必须调用 send_message_to_session 把问题、失败说明或最终结果发送给父会话。"
        f"target_session_id={parent_session_id}。"
        f"{progress_instruction}"
        f"这是第 {attempt} 次通信恢复；不要只再次输出普通最终文本。"
    )


def has_valid_delegated_report(
    successful_tool_calls: list[SuccessfulToolCall],
    *,
    parent_session_id: str,
    allowed_kinds: frozenset[str] = frozenset({"question", "result"}),
) -> bool:
    return any(
        call.tool_name == "send_message_to_session"
        and call.tool_args.get("target_session_id") == parent_session_id
        and call.tool_args.get("kind", "result") in allowed_kinds
        for call in successful_tool_calls
    )


def has_valid_session_question_reply(
    successful_tool_calls: list[SuccessfulToolCall],
    *,
    sender_session_id: str,
    communication_id: str,
) -> bool:
    return any(
        call.tool_name == "send_message_to_session"
        and call.tool_args.get("target_session_id") == sender_session_id
        and call.tool_args.get("kind", "result") == "reply"
        and call.tool_args.get("reply_to_communication_id") == communication_id
        for call in successful_tool_calls
    )


def custom_tools_requested_by_message(
    message: str,
    configured_custom_tool_names: set[str],
) -> set[str]:
    return {
        tool_name
        for tool_name in configured_custom_tool_names
        if tool_name and tool_name in message
    }


def build_missing_custom_tool_retry_reminder(
    *,
    missing_tool_names: set[str],
    attempt: int,
) -> str:
    tools_text = "、".join(sorted(missing_tool_names))
    return (
        "上一轮模型输出了最终正文，但本轮用户请求明确要求执行以下工作区扩展工具，"
        f"而这些工具还没有完成真实工具调用：{tools_text}。"
        f"这是第 {attempt} 次扩展工具调用恢复。"
        "必须通过工具调用通道调用 invoke_extension_tool，"
        '参数格式为 {"tool_name": "<目标扩展工具名>", "arguments": {}}。'
        "不要只描述调用计划，不要把工具名称或 JSON 参数写成普通正文。"
        "工具返回后，最终回复只能包含用户需要看到的结果。"
    )


def internal_retry_human_message(
    *,
    message_id: str,
    kind: str,
    reminder: str,
    metadata: Mapping[str, object],
) -> HumanMessage:
    prepared = internal_message_factory.build(
        kind=kind,
        control=reminder,
        sections=(PromptSection("control_context", metadata),),
        metadata=metadata,
    )
    return HumanMessage(
        id=message_id,
        content=prepared.content,
        response_metadata=prepared.metadata,
    )


__all__ = [
    "build_delegated_report_retry_reminder",
    "build_empty_response_retry_reminder",
    "build_missing_custom_tool_retry_reminder",
    "cancelled_error_reason",
    "custom_tools_requested_by_message",
    "has_valid_delegated_report",
    "has_valid_session_question_reply",
    "internal_retry_human_message",
]
