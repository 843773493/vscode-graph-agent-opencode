from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.agents.tool_call_recovery import (
    extract_pseudo_tool_call,
    find_agent_tool,
    format_recovered_tool_result,
    safe_final_text,
)
from app.core.job_context import set_active_tool_name, set_interruptible_phase
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.services.orchestration.agent_stream_helpers import extract_tool_result_text


async def recover_or_sanitize_final_text(
    *,
    agent: Any,
    final_text: str,
    session_id: str,
    agent_id: str,
    job_id: str,
    publish: Callable[[str, dict[str, Any]], Awaitable[None]],
    logger: Any,
) -> str:
    pseudo_tool_call = extract_pseudo_tool_call(final_text)
    if pseudo_tool_call is None:
        safe_text = safe_final_text(final_text)
        if safe_text != final_text:
            logger.error(
                "[agent_execution_service] blocked unexecuted tool call text: job_id=%s raw=%r",
                job_id,
                final_text,
            )
        return safe_text

    tool_name, tool_args = pseudo_tool_call
    tool = find_agent_tool(agent, tool_name)
    if tool is None:
        raise RuntimeError(
            f"模型返回了未执行的工具调用片段，但当前 agent 没有该工具: {tool_name}"
        )
    logger.warning(
        "[agent_execution_service] recovering pseudo tool call text: job_id=%s tool=%s args=%s",
        job_id,
        tool_name,
        tool_args,
    )
    SessionInterruptState.set(
        session_id,
        phase="tool",
        tool_name=tool_name,
    )
    set_interruptible_phase("tool")
    set_active_tool_name(tool_name)
    await publish(EventType.TOOL_CALL_START, {
        "tool_name": tool_name,
        "args": tool_args,
        "agent_id": agent_id,
    })
    tool_result = await tool.ainvoke(tool_args)
    await publish(EventType.TOOL_CALL_END, {
        "tool_name": tool_name,
        "result": extract_tool_result_text(tool_result),
        "agent_id": agent_id,
    })
    SessionInterruptState.set(session_id, phase=None, tool_name=None)
    set_interruptible_phase("text")
    set_active_tool_name(None)
    return format_recovered_tool_result(tool_name, tool_result)
