"""有序历史 detail 纯投影，不执行 I/O。"""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.trace import TraceEventDTO
from app.schemas.internal_v2.turn import (
    TurnActivityStatsDTO,
    TurnDetailDTO,
    TurnResponsePartDTO,
    TurnThinkingBlockDTO,
    TurnToolSummaryDTO,
)
from app.services.mapping.itemized.history_turns.content import (
    duration_milliseconds,
    final_message,
    is_internal,
    message_id,
    message_time,
    nonnegative_int,
    projection_time,
    thinking_blocks,
    user_message,
    visible_content_text,
)
from app.services.mapping.itemized.history_turns.tools import (
    bounded_tool_summary,
    tool_event,
    tool_items,
)


def build_detail(
    session_id: str,
    ordinal: int,
    messages: list[BaseMessage],
    *,
    turn_id: str,
    message_sequences: list[int] | None = None,
    projection: Mapping[str, object] | None = None,
    response_parts: list[TurnResponsePartDTO] | None = None,
    tool_call_ids: frozenset[str] | None = None,
    include_final: bool = True,
) -> TurnDetailDTO:
    user_messages = [
        message
        for message in messages
        if isinstance(message, HumanMessage) and not is_internal(message)
    ]
    first = user_messages[0] if user_messages else next(iter(messages), None)
    created_at = message_time(
        first,
        fallback=projection_time(projection, "created_at") if projection else None,
    )
    final = (
        final_message(
            messages,
            projection,
            message_sequences=message_sequences,
        )
        if include_final
        and (
            projection is None
            or projection.get("final_message_sequence") in (message_sequences or ())
        )
        else None
    )
    updated_at = (
        projection_time(projection, "updated_at")
        if projection is not None
        else message_time(final, fallback=created_at)
    )
    # v2 history 只承认 TurnRecord.final_item_id 经过 SQLite projection
    # 得到的显式 final pointer。没有 pointer 时表示尚未 terminal convergence
    # 或 completed_empty，不能从最后一条 AIMessage 猜测 final response。
    final_text = visible_content_text(final)
    final_text_truncated = False
    if include_final and projection is not None:
        # catalog 已校验 canonical final item、Turn 和 message identity。
        # 默认 history 只读它的有界 projection，不为 final 文本展开 JSONL。
        projected_final = projection.get("final_response_text")
        if not isinstance(projected_final, str):
            raise RuntimeError("历史索引缺少 final_response_text")
        final_text = projected_final
        final_text_truncated = projection.get("final_response_text_truncated") is True
    assistant_text = [
        text
        for message in messages
        if isinstance(message, AIMessage)
        and message is not final
        and not is_internal(message)
        and (text := visible_content_text(message))
    ]
    projected_thinking = thinking_blocks(messages)
    if projection is not None:
        raw_thinking = projection.get("thinking_blocks")
        if isinstance(raw_thinking, list):
            projected_thinking = [
                TurnThinkingBlockDTO(
                    kind=item["kind"],
                    # SQLite 投影保存完整 reasoning 摘要；DTO 只允许
                    # 有界文本，避免真实模型的长思考内容在组装详情时
                    # 先于统一 budget 截断触发校验异常。
                    text=item.get("text", "")[:4096],
                )
                for item in raw_thinking
                if isinstance(item, Mapping)
                and item.get("kind") in {"reasoning", "summary", "encrypted"}
                and isinstance(item.get("text", ""), str)
            ]
        if projection.get("has_encrypted_reasoning") is True and not any(
            block.kind == "encrypted" for block in projected_thinking
        ):
            projected_thinking.append(TurnThinkingBlockDTO(kind="encrypted"))
    decoded_tool_items = tool_items(
        session_id,
        turn_id,
        messages,
        fallback_timestamp=created_at,
        tool_call_ids=tool_call_ids,
    )
    projected_tool_summary: list[TurnToolSummaryDTO] = []
    projected_tool_items: list[TraceEventDTO] = []
    raw_tool_items = projection.get("tool_items") if projection is not None else None
    if isinstance(raw_tool_items, list):
        projected_call_ids: dict[str, list[str]] = {}
        projected_call_ids_all: list[str] = []
        for raw_item in raw_tool_items:
            if (
                not isinstance(raw_item, Mapping)
                or raw_item.get("item_kind") != "tool_call"
            ):
                continue
            tool_name = raw_item.get("tool_name")
            tool_call_id = raw_item.get("tool_call_id")
            if tool_call_ids is not None and (
                not isinstance(tool_call_id, str) or tool_call_id not in tool_call_ids
            ):
                continue
            if (
                isinstance(tool_name, str)
                and isinstance(tool_call_id, str)
                and tool_name
                and tool_call_id
            ):
                projected_call_ids.setdefault(tool_name, []).append(tool_call_id)
                projected_call_ids_all.append(tool_call_id)
        for index, raw_item in enumerate(raw_tool_items):
            if not isinstance(raw_item, Mapping):
                continue
            tool_name = raw_item.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name:
                tool_name = "tool"
            status = raw_item.get("status")
            if not isinstance(status, str) or not status:
                status = "unknown"
            tool_call_id = raw_item.get("tool_call_id")
            tool_call_id = (
                tool_call_id
                if isinstance(tool_call_id, str) and tool_call_id
                else (
                    projected_call_ids.get(tool_name, []).pop(0)
                    if raw_item.get("item_kind") == "tool_result"
                    and projected_call_ids.get(tool_name)
                    else (
                        projected_call_ids_all.pop(0)
                        if raw_item.get("item_kind") == "tool_result"
                        and projected_call_ids_all
                        else f"{turn_id}:tool:{index}"
                    )
                )
            )
            if tool_call_ids is not None and (
                not isinstance(tool_call_id, str) or tool_call_id not in tool_call_ids
            ):
                continue
            if raw_item.get("item_kind") == "tool_result":
                projected_call_ids_all = [
                    value for value in projected_call_ids_all if value != tool_call_id
                ]
            projected_tool_summary.append(
                TurnToolSummaryDTO(
                    tool_name=tool_name,
                    status=status,
                    tool_call_id=tool_call_id,
                )
            )
            item_kind = raw_item.get("item_kind")
            event_type = (
                "tool_call_start" if item_kind == "tool_call" else "tool_call_end"
            )
            projected_tool_items.append(
                tool_event(
                    event_id=f"{turn_id}:tool_summary:{index}",
                    turn_id=turn_id,
                    event_type=event_type,
                    title=f"{'调用工具' if event_type == 'tool_call_start' else '工具结果'} {tool_name}",
                    tool_name=tool_name,
                    part_id=tool_call_id,
                    timestamp=created_at,
                    raw={},
                    session_id=session_id,
                )
            )
    has_materialized_tool_result = any(
        isinstance(message, ToolMessage) for message in messages
    )
    selected_tool_items = (
        decoded_tool_items
        if has_materialized_tool_result or not projected_tool_items
        else projected_tool_items
    )
    activity_stats = TurnActivityStatsDTO()
    raw_activity_stats = projection.get("activity_stats") if projection else None
    if isinstance(raw_activity_stats, Mapping):
        duration_ms = duration_milliseconds(
            projection.get("created_at") if projection else None,
            projection.get("updated_at") if projection else None,
        )
        activity_stats = TurnActivityStatsDTO(
            duration_ms=duration_ms,
            message_count=nonnegative_int(raw_activity_stats.get("message_count")),
        )
    tool_summary, tool_summary_truncated = bounded_tool_summary(
        projected_tool_summary
        or [
            TurnToolSummaryDTO(
                tool_name=item.tool_name or "tool",
                status=item.status,
                tool_call_id=item.part_id,
            )
            for item in decoded_tool_items
        ]
    )
    detail = TurnDetailDTO(
        turn_id=turn_id,
        job_id=turn_id,
        session_id=session_id,
        ordinal=ordinal,
        revision=1,
        status=JobStatus(
            str(projection.get("status", JobStatus.completed.value))
            if projection is not None
            else JobStatus.completed.value
        ),
        created_at=created_at,
        updated_at=updated_at,
        completed_at=updated_at,
        source_message_ids=[
            message_id(item, f"{turn_id}:user:{index}")
            for index, item in enumerate(user_messages)
        ],
        merged_job_ids=[],
        user_messages=[
            user_message(session_id, turn_id, item, index)
            for index, item in enumerate(user_messages)
        ],
        response_preview=final_text[:1000],
        preview_truncated=len(final_text) > 1000 or final_text_truncated,
        assistant_text=assistant_text,
        thinking_blocks=projected_thinking,
        tool_summary=tool_summary,
        tool_summary_truncated=tool_summary_truncated,
        final_response=final_text,
        response_parts=response_parts or [],
        items=selected_tool_items,
        detail_truncated=tool_summary_truncated or final_text_truncated,
        activity_stats=activity_stats,
    )
    return detail
