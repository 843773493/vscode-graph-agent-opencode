from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import time
from typing import Any

from app.core.job_context import set_active_tool_name, set_interruptible_phase
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.services.mapping.agent_content_mapper import split_agent_content
from app.services.orchestration.agent_stream_helpers import (
    extract_tool_result_text,
    is_tracked_chat_model_event,
    normalize_tool_args,
)


@dataclass(frozen=True, slots=True)
class AgentEventStreamResult:
    final_text: str
    latest_model_reasoning_text: str
    last_tool_result_text: str


async def process_agent_event_stream(
    *,
    agent: Any,
    input_payload: dict[str, Any],
    config: dict[str, Any],
    session_id: str,
    agent_id: str,
    skill_tool_sources: dict[str, list[str]],
    publish: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> AgentEventStreamResult:
    """消费 DeepAgent 事件流，并发布前端可观察的 trace 事件。"""
    collected_text_parts: list[str] = []
    collected_reasoning_parts: list[str] = []
    latest_model_reasoning_parts: list[str] = []
    active_tool_call_id: str | None = None
    active_tool_name: str | None = None
    active_tool_args: dict[str, Any] = {}
    last_tool_result_text = ""

    async for event in agent.astream_events(input_payload, config=config, version="v2"):
        event_type = event.get("event")
        name = event.get("name", "")
        data = event.get("data", {})
        metadata = event.get("metadata", {})

        if event_type == "on_chat_model_start" and is_tracked_chat_model_event(name):
            latest_model_reasoning_parts.clear()
            model_name = metadata.get("ls_model_name") or "unknown_model"
            await publish(
                EventType.LLM_REQUEST,
                {
                    "model": model_name,
                    "timestamp": int(time.time() * 1000),
                },
            )
            continue

        if event_type == "on_chat_model_stream" and is_tracked_chat_model_event(name):
            chunk = data.get("chunk")
            if chunk is None:
                continue
            chunk_message = getattr(chunk, "message", None)
            if chunk_message is not None:
                content = getattr(chunk_message, "content", None) or ""
                tool_calls = getattr(chunk_message, "tool_calls", None) or []
            else:
                content = getattr(chunk, "content", None) or ""
                tool_calls = getattr(chunk, "tool_calls", None) or []

            reasoning_content, text_content = split_agent_content(content)
            if reasoning_content.strip():
                if not collected_text_parts and not collected_reasoning_parts:
                    SessionInterruptState.set(session_id, phase="text", tool_name=None)
                    set_interruptible_phase("text")
                    await publish(EventType.TEXT_START, {})
                collected_reasoning_parts.append(reasoning_content)
                latest_model_reasoning_parts.append(reasoning_content)
                SessionInterruptState.set(
                    session_id,
                    current_text="".join(collected_text_parts),
                )
                await publish(
                    EventType.TEXT_DELTA,
                    {"text": reasoning_content, "kind": "reasoning"},
                )

            if (
                text_content.strip()
                and not collected_text_parts
                and not collected_reasoning_parts
            ):
                SessionInterruptState.set(session_id, phase="text", tool_name=None)
                set_interruptible_phase("text")
                await publish(EventType.TEXT_START, {})

            if text_content and (text_content.strip() or collected_text_parts):
                collected_text_parts.append(text_content)
                SessionInterruptState.set(
                    session_id,
                    current_text="".join(collected_text_parts),
                )
                await publish(EventType.TEXT_DELTA, {"text": text_content, "kind": "text"})

            for tool_call in tool_calls:
                if collected_text_parts or collected_reasoning_parts:
                    collected_text_parts.clear()
                    collected_reasoning_parts.clear()
                    SessionInterruptState.set(session_id, current_text="")
                tool_call_id = tool_call.get("id")
                tool_call_name = tool_call.get("name")
                tool_call_args = tool_call.get("args") or {}
                if tool_call_id and tool_call_id != active_tool_call_id:
                    active_tool_call_id = tool_call_id
                    active_tool_name = tool_call_name
                    active_tool_args = normalize_tool_args(tool_call_args)
                elif tool_call_id == active_tool_call_id and isinstance(tool_call_args, dict):
                    active_tool_args.update(tool_call_args)
            continue

        if event_type == "on_tool_start":
            tool_name = name or active_tool_name or "unknown_tool"
            tool_args = normalize_tool_args(data.get("input"))
            active_tool_name = tool_name
            active_tool_args = tool_args
            skill_names = skill_tool_sources.get(tool_name, [])
            SessionInterruptState.set(session_id, phase="tool", tool_name=tool_name)
            set_interruptible_phase("tool")
            set_active_tool_name(tool_name)
            payload: dict[str, object] = {
                "tool_name": tool_name,
                "args": active_tool_args,
                "agent_id": agent_id,
            }
            if skill_names:
                payload["skill_names"] = skill_names
            await publish(
                EventType.TOOL_CALL_START,
                payload,
            )
            continue

        if event_type == "on_tool_end":
            tool_name = name
            output = data.get("output")
            result_text = extract_tool_result_text(output)
            last_tool_result_text = result_text
            skill_names = skill_tool_sources.get(tool_name, [])
            SessionInterruptState.set(session_id, phase=None, tool_name=None)
            set_interruptible_phase("text")
            set_active_tool_name(None)
            payload = {
                "tool_name": tool_name,
                "result": result_text,
                "agent_id": agent_id,
            }
            if skill_names:
                payload["skill_names"] = skill_names
            await publish(
                EventType.TOOL_CALL_END,
                payload,
            )

    return AgentEventStreamResult(
        final_text="".join(collected_text_parts).strip(),
        latest_model_reasoning_text="".join(latest_model_reasoning_parts),
        last_tool_result_text=last_tool_result_text,
    )
