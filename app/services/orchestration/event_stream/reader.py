"""拥有事件迭代、启动/模型/工具 watchdog 和生成器取消清理。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from app.agents.model_capability_routing import MODEL_FAILED_CUSTOM_EVENT
from app.core.turn_execution_scope import CancellationSignal, ScopeCancelledError
from app.services.orchestration.agent_stream_helpers import is_tracked_chat_model_event
from app.services.orchestration.event_stream.contracts import (
    AgentEventSource,
    AgentEventStreamTimeoutError,
)

if TYPE_CHECKING:
    from app.services.orchestration.message_stream_runtime import MessageStreamRuntime
# 仅限制首次可观察进展；模型 idle watchdog 只在调用方显式配置时启用。
DEFAULT_INITIAL_EVENT_TIMEOUT_SECONDS = 180.0
DEFAULT_TOOL_DISPATCH_TIMEOUT_SECONDS = 30.0


def _is_progress_event(event: dict[str, Any]) -> bool:
    """判断事件流是否已经进入可观察的模型或工具阶段。"""
    event_type = event.get("event")
    name = event.get("name", "")
    if (
        isinstance(event_type, str)
        and event_type.startswith("on_chat_model_")
        and isinstance(name, str)
        and is_tracked_chat_model_event(name)
    ):
        return True
    return event_type in {"on_tool_start", "on_tool_end"} or (
        event_type == "on_custom_event" and name == MODEL_FAILED_CUSTOM_EVENT
    )


def _is_agent_lifecycle_event(event: dict[str, Any]) -> bool:
    """判断事件流是否仍在推进 Agent 生命周期。"""
    event_type = event.get("event")
    return isinstance(event_type, str) and event_type.startswith("on_")


def _is_model_start_event(event: dict[str, Any]) -> bool:
    return (
        event.get("event") == "on_chat_model_start"
        and isinstance(event.get("name"), str)
        and is_tracked_chat_model_event(event["name"])
    )


def _is_model_end_event(event: dict[str, Any]) -> bool:
    return (
        event.get("event") == "on_chat_model_end"
        and isinstance(event.get("name"), str)
        and is_tracked_chat_model_event(event["name"])
    )


def resolve_event_timeouts(
    model_timeout_seconds: float | None,
    tool_dispatch_timeout_seconds: float | None,
) -> tuple[float | None, float, float]:
    """解析事件流的启动、模型空闲和工具分派超时。"""
    if model_timeout_seconds is None:
        idle_model_timeout_seconds = None
        initial_event_timeout_seconds = DEFAULT_INITIAL_EVENT_TIMEOUT_SECONDS
    else:
        if isinstance(model_timeout_seconds, bool) or model_timeout_seconds <= 0:
            raise ValueError("model_timeout_seconds 必须大于 0")
        idle_model_timeout_seconds = model_timeout_seconds
        initial_event_timeout_seconds = model_timeout_seconds
    effective_tool_dispatch_timeout_seconds = (
        DEFAULT_TOOL_DISPATCH_TIMEOUT_SECONDS
        if tool_dispatch_timeout_seconds is None
        else tool_dispatch_timeout_seconds
    )
    if (
        isinstance(effective_tool_dispatch_timeout_seconds, bool)
        or effective_tool_dispatch_timeout_seconds <= 0
    ):
        raise ValueError("tool_dispatch_timeout_seconds 必须大于 0")
    return (
        idle_model_timeout_seconds,
        initial_event_timeout_seconds,
        effective_tool_dispatch_timeout_seconds,
    )


async def iter_agent_events(
    *,
    agent: AgentEventSource,
    input_payload: dict[str, Any],
    stream_config: dict[str, Any],
    session_id: str,
    turn_id: str,
    message_stream_runtime: MessageStreamRuntime | None,
    cancellation_signal: CancellationSignal | None,
    idle_model_timeout_seconds: float | None,
    initial_event_timeout_seconds: float,
    effective_tool_dispatch_timeout_seconds: float,
) -> AsyncIterator[dict[str, Any]]:
    event_iterator = agent.astream_events(
        input_payload,
        config=stream_config,
        version="v2",
    ).__aiter__()
    loop = asyncio.get_running_loop()
    next_event_task: asyncio.Task[dict[str, Any]] | None = None
    timeout_triggered = False
    first_progress_deadline: float | None = loop.time() + initial_event_timeout_seconds
    model_deadline: float | None = None
    tool_dispatch_deadline: float | None = None

    async def raise_stream_timeout(phase: str, *, code: str) -> None:
        if code == "tool_dispatch_timeout" and message_stream_runtime is not None:
            pending = message_stream_runtime.pending_tool_calls()
            if pending:
                await message_stream_runtime.fail_pending_tool_calls(
                    completion_reason=code,
                    error=(
                        "模型工具调用参数已完整，但在有限时间内没有收到工具执行分派事件: "
                        f"tool_calls={[item[0] for item in pending]}"
                    ),
                )
        timeout_seconds = (
            effective_tool_dispatch_timeout_seconds
            if code == "tool_dispatch_timeout"
            else idle_model_timeout_seconds
            if idle_model_timeout_seconds is not None
            else initial_event_timeout_seconds
        )
        raise AgentEventStreamTimeoutError(
            f"Agent 事件流等待{phase}超过 {timeout_seconds:.0f} 秒: "
            f"session_id={session_id} job_id={turn_id}",
            code=code,
        )

    try:
        while True:
            pending_tool_calls = (
                message_stream_runtime.pending_tool_calls()
                if message_stream_runtime is not None
                else ()
            )
            if model_deadline is None and pending_tool_calls:
                if tool_dispatch_deadline is None:
                    tool_dispatch_deadline = (
                        loop.time() + effective_tool_dispatch_timeout_seconds
                    )
            else:
                tool_dispatch_deadline = None
            deadlines = [
                (model_deadline, "模型响应", "agent_event_timeout"),
                (
                    tool_dispatch_deadline,
                    "工具调用分派",
                    "tool_dispatch_timeout",
                ),
                (
                    first_progress_deadline,
                    "首个模型/工具事件",
                    "agent_event_timeout",
                ),
            ]
            active_deadlines = [item for item in deadlines if item[0] is not None]
            if active_deadlines:
                deadline, phase, timeout_code = min(
                    active_deadlines,
                    key=lambda item: item[0] or float("inf"),
                )
            else:
                deadline = None
                phase = "Agent 事件流"
                timeout_code = "agent_event_timeout"
            if deadline is None:
                try:
                    event = await event_iterator.__anext__()
                except StopAsyncIteration:
                    return
            else:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    await raise_stream_timeout(phase, code=timeout_code)
                if next_event_task is None:
                    next_event_task = asyncio.create_task(
                        event_iterator.__anext__(),
                    )
                try:
                    # wait_for 直接包住异步生成器的 __anext__ 时，超时会把
                    # CancelledError 注入 AgentLoop。使用独立 task 配合 wait，
                    # 保证这里的等待超时只产生本地 tool_dispatch_timeout；
                    # 生成器任务在 finally 中单独收束，不能把清理取消冒泡成
                    # execution_lost。
                    done, _ = await asyncio.wait(
                        {next_event_task},
                        timeout=timeout,
                    )
                    if not done:
                        timeout_triggered = True
                        await raise_stream_timeout(phase, code=timeout_code)
                    event = next_event_task.result()
                    next_event_task = None
                except StopAsyncIteration:
                    next_event_task = None
                    return
            yield event
            if _is_progress_event(event):
                first_progress_deadline = None
            elif first_progress_deadline is not None and _is_agent_lifecycle_event(
                event
            ):
                # LangGraph 在首个模型事件前可能先发出 chain/prompt/parser
                # 生命周期事件。它们不是用户可见的模型进展，但说明 AgentLoop
                # 没有卡死；把首事件 watchdog 作为“无事件空闲超时”续期。
                first_progress_deadline = loop.time() + initial_event_timeout_seconds
            if _is_model_start_event(event):
                model_deadline = (
                    loop.time() + idle_model_timeout_seconds
                    if idle_model_timeout_seconds is not None
                    else None
                )
            elif _is_model_end_event(event):
                model_deadline = None
            elif model_deadline is not None:
                # 这是模型空闲 watchdog，而不是从模型开始事件起算的固定
                # 响应总预算。活跃的长推理/流式响应不应被 60 秒硬切。
                model_deadline = loop.time() + idle_model_timeout_seconds
    except ScopeCancelledError as error:
        if cancellation_signal is None or not cancellation_signal.is_cancelled:
            raise
        raise asyncio.CancelledError(str(error)) from error
    finally:
        primary_exception = sys.exc_info()[1]
        if next_event_task is not None and not next_event_task.done():
            next_event_task.cancel("agent_event_stream_cleanup")
        if next_event_task is not None:
            try:
                await next_event_task
            except asyncio.CancelledError:
                if not timeout_triggered and primary_exception is None:
                    raise
            except BaseException:
                if not timeout_triggered and primary_exception is None:
                    raise
        close_iterator = getattr(event_iterator, "aclose", None)
        if callable(close_iterator):
            try:
                await close_iterator()
            except asyncio.CancelledError:
                if not timeout_triggered and primary_exception is None:
                    raise
            except RuntimeError:
                if not timeout_triggered and primary_exception is None:
                    raise
