"""Agent step 的取消、失败与 execution-lost 终态处理。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.job_event_bus import EventType
from app.core.turn_execution_scope import ScopeCancelledError
from app.services.business.system_reminder_checkpoint_service import (
    persist_interrupt_checkpoint,
)
from app.services.orchestration.event_stream.contracts import (
    AgentEventStreamTimeoutError,
)
from app.services.orchestration.execution_step.reminders import cancelled_error_reason

STRUCTURED_CANCELLATION_REASONS = frozenset(
    {
        "job_startup_timeout",
        "job_timeout",
        "scope_deadline_exceeded",
        "tool_dispatch_timeout",
    }
)


async def handle_step_cancelled(
    *,
    cancellation_error: asyncio.CancelledError,
    state: Any,
    message_stream_runtime: Any,
    message_stream_writer: Any,
    checkpointer: Any,
    session_id: str,
    turn_id: str,
    checkpoint_ns: str,
    logger: logging.Logger,
) -> None:
    """完成取消事实收口；调用方随后重新抛出 CancelledError。"""
    user_interrupt = state.interrupt_request_id is not None
    cancellation_reason = cancelled_error_reason(cancellation_error)
    pending_tool_calls = message_stream_runtime.pending_tool_calls()
    complete_pending_tool_call_ids = [
        tool_call_id
        for tool_call_id, _tool_name, arguments_complete in pending_tool_calls
        if arguments_complete
    ]
    failure_code = (
        "user_interrupt"
        if user_interrupt
        else "job_startup_timeout"
        if cancellation_reason == "job_startup_timeout"
        else "job_timeout"
        if cancellation_reason == "job_timeout"
        else "scope_deadline_exceeded"
        if cancellation_reason == "scope_deadline_exceeded"
        else "tool_dispatch_timeout"
        if complete_pending_tool_call_ids
        else "execution_lost"
    )
    failure_message = (
        "用户中断后的 AgentLoop 失败"
        if user_interrupt
        else "Job 启动超过等待首个模型/工具事件的上限"
        if failure_code == "job_startup_timeout"
        else "Job 执行超过总超时上限"
        if failure_code == "job_timeout"
        else (
            "模型工具调用参数已完整，但工具执行分派在取消前没有启动: "
            f"tool_calls={complete_pending_tool_call_ids}"
        )
        if failure_code == "tool_dispatch_timeout"
        else "AgentLoop 因内部取消而结束，未收到用户中断请求"
    )
    if not (user_interrupt and state.user_interrupt_reminder_injected):
        try:
            await asyncio.to_thread(
            persist_interrupt_checkpoint,
            checkpointer=checkpointer,
            session_id=session_id,
            active_tool_name=state.tool_name,
                checkpoint_source=("interrupt" if user_interrupt else failure_code),
                event_identity=turn_id,
            )
            logger.info(
                "[agent_execution_service] cancellation checkpoint persisted: "
                "turn_id=%s code=%s",
                turn_id,
                failure_code,
            )
        except Exception:
            logger.exception(
                "[agent_execution_service] cancellation checkpoint persistence failed: "
                "turn_id=%s",
                turn_id,
            )
    if user_interrupt:
        if state.user_interrupt_reminder_injected:
            logger.info(
                "[agent_execution_service] job cancelled after user interrupt "
                "reminder persisted: turn_id=%s",
                turn_id,
            )
        await message_stream_runtime.finalize_interruption_facts()
        await message_stream_writer.close_interrupted(state.interrupt_request_id)
        return
    await message_stream_runtime.fail_pending_tool_calls(
        completion_reason=failure_code,
        error=failure_message,
    )
    mark_execution_lost = getattr(checkpointer, "mark_execution_lost", None)
    if not callable(mark_execution_lost):
        raise TypeError("v2 checkpoint saver 缺少可调用端口: mark_execution_lost")
    await asyncio.to_thread(
        mark_execution_lost,
        session_id,
        turn_id=turn_id,
        reason=failure_code,
        checkpoint_ns=checkpoint_ns,
    )
    await message_stream_writer.close_failed(
        code=failure_code,
        message=failure_message,
        resumable=False,
    )


async def handle_step_failure(
    *,
    error: Exception,
    interrupt_state: Any,
    message_stream_runtime: Any,
    message_stream_writer: Any,
    checkpointer: Any,
    session_id: str,
    turn_id: str,
    checkpoint_ns: str,
    publish: Callable[[str, dict[str, Any]], Awaitable[None]],
    logger: logging.Logger,
) -> None:
    """把普通异常映射为 v2 execution-lost/stream failure 终态。"""
    if interrupt_state.interrupt_request_id is not None:
        await message_stream_runtime.finalize_interruption_facts()
        return
    failure_code = (
        error.code
        if isinstance(error, AgentEventStreamTimeoutError)
        else error.reason
        if isinstance(error, ScopeCancelledError)
        and error.reason in STRUCTURED_CANCELLATION_REASONS
        else "execution_lost"
        if isinstance(error, ScopeCancelledError)
        else "execution_error"
    )
    failure_message = str(error)
    if isinstance(error, ScopeCancelledError):
        try:
            await asyncio.to_thread(
            persist_interrupt_checkpoint,
            checkpointer=checkpointer,
            session_id=session_id,
            active_tool_name=interrupt_state.tool_name,
                checkpoint_source=failure_code,
                event_identity=turn_id,
            )
        except Exception:
            logger.exception(
                "[agent_execution_service] scope failure checkpoint persistence "
                "failed: turn_id=%s",
                turn_id,
            )
    await message_stream_runtime.fail_pending_tool_calls(
        completion_reason=failure_code,
        error=failure_message,
    )
    await message_stream_runtime.fail_model(
        code=failure_code,
        message=failure_message,
    )
    mark_execution_lost = getattr(checkpointer, "mark_execution_lost", None)
    if not callable(mark_execution_lost):
        raise TypeError("v2 checkpoint saver 缺少可调用端口: mark_execution_lost")
    await asyncio.to_thread(
        mark_execution_lost,
        session_id,
        turn_id=turn_id,
        reason=failure_code,
        checkpoint_ns=checkpoint_ns,
    )
    await message_stream_writer.close_failed(
        code=(
            "user_interrupt"
            if interrupt_state.interrupt_request_id is not None
            else failure_code
        ),
        message=(
            "用户中断后的 AgentLoop 失败"
            if interrupt_state.interrupt_request_id is not None
            else failure_message
        ),
        after_interrupt_requested=bool(interrupt_state.cancellation_reason),
        resumable=False,
    )
    await publish(EventType.ERROR, {"error": str(error), "phase": "agent_execution"})
    logger.exception(
        "[agent_execution_service] ERROR published: turn_id=%s",
        turn_id,
    )


__all__ = [
    "STRUCTURED_CANCELLATION_REASONS",
    "handle_step_cancelled",
    "handle_step_failure",
]
