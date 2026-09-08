"""有序历史 tools 纯投影，不执行 I/O。"""

from __future__ import annotations

from datetime import datetime

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.schemas.internal_v2.trace import TraceEventDTO
from app.schemas.internal_v2.turn import (
    TurnToolSummaryDTO,
)
from app.services.mapping.itemized.history_turns.content import (
    content_text,
    message_time,
)

_TOOL_SUMMARY_LIMIT = 64


def tool_items(
    session_id: str,
    turn_id: str,
    messages: list[BaseMessage],
    *,
    fallback_timestamp: datetime,
    tool_call_ids: frozenset[str] | None = None,
) -> list[TraceEventDTO]:
    items: list[TraceEventDTO] = []
    tool_names: dict[str, str] = {}
    tool_ids_by_name: dict[str, list[str]] = {}
    tool_ids: list[str] = []
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            call_id = call.get("id")
            if tool_call_ids is not None and (
                not isinstance(call_id, str) or call_id not in tool_call_ids
            ):
                continue
            name = call.get("name")
            if isinstance(call_id, str) and isinstance(name, str) and name:
                tool_names[call_id] = name
                tool_ids_by_name.setdefault(name, []).append(call_id)
                tool_ids.append(call_id)
    for message_index, message in enumerate(messages):
        timestamp = message_time(message, fallback=fallback_timestamp)
        if isinstance(message, AIMessage):
            for call_index, call in enumerate(message.tool_calls or []):
                name = str(call.get("name") or "unknown_tool")
                args = call.get("args", {})
                call_id = str(call.get("id") or f"{turn_id}:call:{call_index}")
                if tool_call_ids is not None and call_id not in tool_call_ids:
                    continue
                items.append(
                    tool_event(
                        event_id=f"{turn_id}:tool_call:{message_index}:{call_index}",
                        turn_id=turn_id,
                        event_type="tool_call_start",
                        title=f"调用工具 {name}",
                        tool_name=name,
                        part_id=call_id,
                        timestamp=timestamp,
                        raw={
                            "payload": {
                                "args": args,
                                "id": call_id,
                                "tool_name": name,
                            }
                        },
                        session_id=session_id,
                    )
                )
        elif isinstance(message, ToolMessage):
            name = message.name or tool_names.get(message.tool_call_id) or "tool"
            tool_call_id = message.tool_call_id
            if tool_call_ids is not None and (
                not isinstance(tool_call_id, str) or tool_call_id not in tool_call_ids
            ):
                continue
            if not tool_call_id and tool_ids_by_name.get(name):
                tool_call_id = tool_ids_by_name[name].pop(0)
            if not tool_call_id and tool_ids:
                tool_call_id = tool_ids.pop(0)
            if tool_call_id in tool_ids:
                tool_ids.remove(tool_call_id)
            tool_call_id = tool_call_id or f"{turn_id}:tool-result:{message_index}"
            items.append(
                tool_event(
                    event_id=f"{turn_id}:tool_result:{message_index}",
                    turn_id=turn_id,
                    event_type="tool_call_end",
                    title=f"工具结果 {name}",
                    tool_name=name,
                    part_id=tool_call_id,
                    timestamp=timestamp,
                    raw={
                        "payload": {
                            "result": content_text(message),
                            "tool_call_id": tool_call_id,
                            "tool_name": name,
                        }
                    },
                    session_id=session_id,
                )
            )
    return items


def tool_event(
    *,
    event_id: str,
    turn_id: str,
    event_type: str,
    title: str,
    tool_name: str,
    part_id: str | None,
    timestamp: datetime,
    raw: dict[str, object],
    session_id: str,
) -> TraceEventDTO:
    return TraceEventDTO(
        event_id=event_id,
        part_id=part_id,
        session_id=session_id,
        job_id=turn_id,
        type=event_type,
        phase="tool",
        title=title,
        content="",
        status="completed",
        tool_name=tool_name,
        timestamp=timestamp,
        raw=raw,
    )


def bounded_tool_summary(
    items: list[TurnToolSummaryDTO],
) -> tuple[list[TurnToolSummaryDTO], bool]:
    if len(items) <= _TOOL_SUMMARY_LIMIT:
        return items, False
    head_count = _TOOL_SUMMARY_LIMIT // 2
    tail_count = _TOOL_SUMMARY_LIMIT - head_count
    return [*items[:head_count], *items[-tail_count:]], True
