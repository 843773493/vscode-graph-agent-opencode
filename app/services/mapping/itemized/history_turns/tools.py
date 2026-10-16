"""有序历史 tools 纯投影，不执行 I/O。"""

from __future__ import annotations

from datetime import datetime

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import (
    matches_tool_call_id,
    provider_tool_call_id,
)
from app.schemas.internal_v2.trace import TraceEventDTO
from app.schemas.internal_v2.turn import (
    TurnToolSummaryDTO,
)
from app.services.mapping.itemized.history_turns.content import (
    content_text,
    message_time,
)

_TOOL_SUMMARY_LIMIT = 64


def history_tool_call_id(identity: ItemProjectionIdentity, call_id: str) -> str:
    """历史聚合引用携带 emitting ModelCall；Provider 消息仍使用原始 call ID。"""
    raw_call_id = provider_tool_call_id(identity, call_id)
    return (
        f"{identity.model_call_id}:tool-call:{raw_call_id}"
        if identity.model_call_id is not None else raw_call_id
    )


def tool_items(
    session_id: str,
    turn_id: str,
    messages: list[BaseMessage],
    *,
    job_id: str,
    fallback_timestamp: datetime,
    tool_call_ids: frozenset[str] | None = None,
) -> list[TraceEventDTO]:
    items: list[TraceEventDTO] = []
    tool_names: dict[tuple[str | None, str], str] = {}
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        identity = ItemProjectionIdentity.from_dict(
            {"model_call_id": message.response_metadata.get("model_call_id")}
        )
        for call in message.tool_calls or []:
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id:
                if tool_call_ids is not None:
                    continue
                raise ValueError("历史 AI 工具调用缺少非空 tool_call_id")
            if tool_call_ids is not None and (
                not any(matches_tool_call_id(identity, call_id, selected_id) for selected_id in tool_call_ids)
            ):
                continue
            name = call.get("name")
            if isinstance(name, str) and name:
                tool_names[(identity.model_call_id, provider_tool_call_id(identity, call_id))] = name
    for message_index, message in enumerate(messages):
        timestamp = message_time(message, fallback=fallback_timestamp)
        identity = ItemProjectionIdentity.from_dict(
            {"model_call_id": message.response_metadata.get("model_call_id")}
        )
        if isinstance(message, AIMessage):
            for call_index, call in enumerate(message.tool_calls or []):
                name = str(call.get("name") or "unknown_tool")
                args = call.get("args", {})
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    if tool_call_ids is not None:
                        continue
                    raise ValueError("历史 AI 工具调用缺少非空 tool_call_id")
                if tool_call_ids is not None and not any(
                    matches_tool_call_id(identity, call_id, selected_id)
                    for selected_id in tool_call_ids
                ):
                    continue
                items.append(
                    tool_event(
                        event_id=f"{turn_id}:tool_call:{message_index}:{call_index}",
                        job_id=job_id,
                        event_type="tool_call_start",
                        title=f"调用工具 {name}",
                        tool_name=name,
                        part_id=history_tool_call_id(identity, call_id),
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
            tool_call_id = message.tool_call_id
            if not isinstance(tool_call_id, str) or not tool_call_id:
                if tool_call_ids is not None:
                    continue
                raise ValueError("历史工具结果缺少非空 tool_call_id")
            if tool_call_ids is not None and (
                not any(matches_tool_call_id(identity, tool_call_id, selected_id) for selected_id in tool_call_ids)
            ):
                continue
            name = message.name or tool_names.get(
                (identity.model_call_id, provider_tool_call_id(identity, tool_call_id))
            ) or "tool"
            items.append(
                tool_event(
                    event_id=f"{turn_id}:tool_result:{message_index}",
                    job_id=job_id,
                    event_type="tool_call_end",
                    title=f"工具结果 {name}",
                    tool_name=name,
                    part_id=history_tool_call_id(identity, tool_call_id),
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
    job_id: str,
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
        job_id=job_id,
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
