"""把 canonical rollout 消息映射为历史和 live 共用的响应部件。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Literal

from app.core.message_content_projection import reasoning_projection_rows
from app.schemas.internal_v2.turn import TurnResponsePartDTO, TurnResponseSourceDTO

Projection = Literal["summary", "detail", "streaming"]

_TERMINAL_TURN_STATUSES = {
    "completed",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
}


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    if isinstance(value, Mapping):
        candidate = value.get("text")
        return candidate if isinstance(candidate, str) else ""
    return ""


def _blocks(content: object) -> list[Mapping[str, object]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, Mapping)]


def _serialized_message(record: Mapping[str, object]) -> Mapping[str, object]:
    message = record.get("message")
    if not isinstance(message, Mapping):
        raise TypeError("rollout message record 缺少 message")
    return message


def _message_data(message: Mapping[str, object]) -> Mapping[str, object]:
    data = message.get("data")
    if not isinstance(data, Mapping):
        raise TypeError("rollout message record 缺少 data")
    return data


def _completion_metadata(record: Mapping[str, object]) -> tuple[str | None, bool]:
    """读取 AIMessage 的 block 收尾语义，不把 partial 当作正常完成。"""
    metadata = _message_data(_serialized_message(record)).get("response_metadata")
    if not isinstance(metadata, Mapping):
        return None, False
    reason = metadata.get("completion_reason")
    return (
        reason if isinstance(reason, str) and reason else None,
        metadata.get("partial") is True,
    )


def _completion_metadata_for_sequence(
    records: Sequence[Mapping[str, object]],
    sequence: object,
) -> tuple[str | None, bool]:
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        return None, False
    for record in records:
        if record.get("_indexed_sequence") == sequence:
            return _completion_metadata(record)
    return None, False


def _bounded(value: object, limit: int = 65536) -> tuple[str, bool]:
    text = _text(value)
    return text[:limit], len(text) > limit


def _summary_tool_parts(
    projection: Mapping[str, object],
    *,
    limit: int,
    include: frozenset[str],
) -> list[TurnResponsePartDTO]:
    raw_items = projection.get("tool_items")
    if not isinstance(raw_items, list):
        return []
    projection_status = projection.get("status")
    terminal_turn = (
        isinstance(projection_status, str)
        and projection_status in _TERMINAL_TURN_STATUSES
    )
    result_call_ids = {
        raw_item.get("tool_call_id")
        for raw_item in raw_items
        if isinstance(raw_item, Mapping)
        and raw_item.get("item_kind") == "tool_result"
        and isinstance(raw_item.get("tool_call_id"), str)
    }
    parts: list[TurnResponsePartDTO] = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            continue
        item_kind = raw_item.get("item_kind")
        if item_kind not in {"tool_call", "tool_result"}:
            continue
        if item_kind == "tool_call":
            if not ({"tool_summary", "tool_call"} & include):
                continue
        elif "tool_result" not in include:
            continue
        sequence = raw_item.get("sequence")
        assistant_sequence = raw_item.get("assistant_message_sequence")
        call_index = raw_item.get("call_index")
        call_id = raw_item.get("tool_call_id")
        tool_name = raw_item.get("tool_name")
        status = raw_item.get("status")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            continue
        if not isinstance(assistant_sequence, int) or isinstance(assistant_sequence, bool):
            assistant_sequence = sequence
        if not isinstance(call_index, int) or isinstance(call_index, bool):
            call_index = None
        if not isinstance(call_id, str) or not call_id:
            call_id = None
        if not isinstance(tool_name, str) or not tool_name:
            tool_name = "tool"
        normalized_status = status if isinstance(status, str) and status else "unknown"
        terminal_failure = normalized_status in {"failed", "error"}
        terminal_success = normalized_status in {
            "completed",
            "ok",
            "success",
            "succeeded",
        }
        outcome_unknown = (
            item_kind == "tool_call"
            and terminal_turn
            and call_id not in result_call_ids
        )
        parts.append(
            TurnResponsePartDTO(
                part_id=f"tool-call:{assistant_sequence}:{call_index or 0}",
                kind="tool_call" if item_kind == "tool_call" else "tool_result",
                projection="summary",
                status=(
                    "failed"
                    if terminal_failure or outcome_unknown
                    else "completed"
                    if item_kind == "tool_result" or terminal_success
                    else "pending"
                ),
                source=TurnResponseSourceDTO(
                    message_sequence=sequence,
                    assistant_message_sequence=assistant_sequence,
                    call_index=call_index,
                    result_message_sequence=(
                        sequence if item_kind == "tool_result" else None
                    ),
                ),
                text=normalized_status[:limit],
                tool_call_id=call_id,
                tool_name=tool_name,
                outcome_unknown=outcome_unknown,
            )
        )
    return parts


def response_parts_from_records(
    records: Sequence[Mapping[str, object]],
    *,
    projection: Mapping[str, object] | None,
    mode: Projection,
    include: frozenset[str],
    tool_call_ids: frozenset[str] | None = None,
    max_parts: int = 512,
) -> list[TurnResponsePartDTO]:
    """按 canonical 顺序生成 response parts。

    summary 模式以 SQLite 投影为正文来源，并从命中的最终 JSONL record 补充
    partial/completion_reason；detail 模式读取命中的 JSONL records。
    """
    if mode == "summary":
        if projection is None:
            return []
        parts: list[TurnResponsePartDTO] = []
        final_sequence = projection.get("final_message_sequence")
        if isinstance(final_sequence, int) and not isinstance(final_sequence, bool):
            final_text = projection.get("final_response_text")
            if isinstance(final_text, str) and final_text:
                completion_reason, partial = _completion_metadata_for_sequence(
                    records,
                    final_sequence,
                )
                parts.append(
                    TurnResponsePartDTO(
                        part_id=f"message:{final_sequence}:final",
                        kind="text" if partial else "final_text",
                        projection="summary",
                        source=TurnResponseSourceDTO(message_sequence=final_sequence),
                        text=final_text[:65536],
                        truncated=bool(projection.get("final_response_text_truncated")),
                        final=not partial,
                        completion_reason=completion_reason,
                        partial=partial,
                    )
                )
        raw_blocks = projection.get("thinking_blocks")
        if isinstance(raw_blocks, list):
            for index, raw in enumerate(raw_blocks):
                if not isinstance(raw, Mapping):
                    continue
                kind = raw.get("kind")
                if kind not in {"reasoning", "summary", "encrypted"}:
                    continue
                if kind == "reasoning" and not include & {"thinking", "reasoning_detail"}:
                    continue
                if kind == "summary" and not include & {
                    "thinking",
                    "reasoning_summary",
                    "reasoning_detail",
                }:
                    continue
                if kind == "encrypted" and "encrypted_reasoning_meta" not in include:
                    continue
                sequence = raw.get("message_sequence")
                if not isinstance(sequence, int) or isinstance(sequence, bool):
                    sequence = final_sequence if isinstance(final_sequence, int) else 1
                part_kind = (
                    "reasoning_summary"
                    if kind == "summary"
                    else "reasoning_encrypted"
                    if kind == "encrypted"
                    else "reasoning"
                )
                parts.append(
                    TurnResponsePartDTO(
                        part_id=f"reasoning:{sequence}:{index}",
                        kind=part_kind,
                        projection="summary",
                        source=TurnResponseSourceDTO(
                            message_sequence=sequence,
                            content_block_index=(
                                raw.get("content_block_index")
                                if isinstance(raw.get("content_block_index"), int)
                                else None
                            ),
                            item_index=(
                                raw.get("item_index")
                                if isinstance(raw.get("item_index"), int)
                                else None
                            ),
                        ),
                        text=(
                            "思考内容已加密"
                            if kind == "encrypted"
                            else str(raw.get("text") or "")[:65536]
                        ),
                        carrier_type=(
                            raw.get("carrier_type")
                            if isinstance(raw.get("carrier_type"), str)
                            else None
                        ),
                    )
                )
        parts.extend(
            _summary_tool_parts(
                projection,
                limit=65536,
                include=include,
            )
        )
        parts.sort(
            key=lambda part: (
                part.source.message_sequence,
                part.source.content_block_index
                if part.source.content_block_index is not None
                else 1_000_000_000,
                part.source.item_index
                if part.source.item_index is not None
                else 1_000_000_000,
                part.source.call_index
                if part.source.call_index is not None
                else 1_000_000_000,
                1 if part.kind == "tool_result" else 0,
            )
        )
        return parts[:max_parts]

    parts = []
    tool_call_sources: dict[str, list[tuple[int, int]]] = {}
    terminal_turn = (
        isinstance(projection, Mapping)
        and isinstance(projection.get("status"), str)
        and projection.get("status") in _TERMINAL_TURN_STATUSES
    )
    result_call_ids = {
        data.get("tool_call_id")
        for record in records
        if isinstance(record, Mapping)
        and _serialized_message(record).get("type") == "tool"
        for data in [_message_data(_serialized_message(record))]
        if isinstance(data.get("tool_call_id"), str)
    }
    final_sequence = (
        projection.get("final_message_sequence") if projection is not None else None
    )
    for record in records:
        sequence = record.get("_indexed_sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            continue
        message = _serialized_message(record)
        message_type = message.get("type")
        data = _message_data(message)
        if message_type == "ai":
            content = data.get("content")
            blocks = _blocks(content)
            reasoning_rows = reasoning_projection_rows(content)
            completion_reason, partial = _completion_metadata(record)
            for block_index, block in enumerate(blocks):
                block_type = block.get("type")
                if block_type in {"text", "input_text", "output_text", "refusal"}:
                    text, truncated = _bounded(
                        block.get("text") or block.get("refusal")
                    )
                    if not text:
                        continue
                    is_final = sequence == final_sequence
                    if is_final and not (
                        {"final_response", "assistant"} & include
                    ):
                        continue
                    if not is_final and "text" not in include:
                        continue
                    parts.append(
                        TurnResponsePartDTO(
                            part_id=f"message:{sequence}:content:{block_index}",
                            kind="final_text" if is_final and not partial else "text",
                            projection="detail",
                            source=TurnResponseSourceDTO(
                                message_sequence=sequence,
                                content_block_index=block_index,
                            ),
                            text=text,
                            truncated=truncated,
                            final=is_final and not partial,
                            completion_reason=completion_reason,
                            partial=partial,
                        )
                    )
                    continue
                block_rows = [
                    row
                    for row in reasoning_rows
                    if row.get("content_block_index") == block_index
                ]
                for row in block_rows:
                    row_index = row.get("item_index")
                    if not isinstance(row_index, int):
                        row_index = 0
                    kind = row.get("kind")
                    text, truncated = _bounded(
                        "思考内容已加密" if kind == "encrypted" else row.get("text")
                    )
                    source = TurnResponseSourceDTO(
                        message_sequence=sequence,
                        content_block_index=block_index,
                        item_index=row_index,
                    )
                    carrier_type = (
                        row.get("carrier_type")
                        if isinstance(row.get("carrier_type"), str)
                        else None
                    )
                    logical_kind = (
                        "reasoning_encrypted"
                        if kind == "encrypted"
                        else "reasoning_summary"
                        if kind == "summary"
                        else "reasoning"
                    )
                    if not text and logical_kind != "reasoning_encrypted":
                        continue
                    parts.append(
                        TurnResponsePartDTO(
                            part_id=(
                                f"message:{sequence}:reasoning:{block_index}:"
                                f"{row_index}"
                            ),
                            kind=logical_kind,
                            projection="detail",
                            source=source,
                            text=text,
                            carrier_type=carrier_type,
                            truncated=truncated,
                            completion_reason=completion_reason,
                            partial=partial,
                        )
                    )
            if "tool_call" in include or "tool_result" in include:
                calls = data.get("tool_calls")
                if isinstance(calls, list):
                    for call_index, call in enumerate(calls):
                        if not isinstance(call, Mapping):
                            continue
                        call_id = call.get("id")
                        name = call.get("name")
                        if not isinstance(call_id, str) or not call_id:
                            continue
                        if tool_call_ids is not None and call_id not in tool_call_ids:
                            continue
                        if not isinstance(name, str) or not name:
                            name = "tool"
                        arguments, truncated = _bounded(
                            json.dumps(
                                call.get("args", {}),
                                ensure_ascii=False,
                                default=str,
                            )
                        )
                        outcome_unknown = terminal_turn and call_id not in result_call_ids
                        if "tool_call" in include:
                            parts.append(
                                TurnResponsePartDTO(
                                    part_id=f"tool-call:{sequence}:{call_index}",
                                    kind="tool_call",
                                    projection="detail",
                                    status="failed" if outcome_unknown else "pending",
                                    source=TurnResponseSourceDTO(
                                        message_sequence=sequence,
                                        assistant_message_sequence=sequence,
                                        call_index=call_index,
                                    ),
                                    tool_call_id=call_id,
                                    tool_name=name,
                                    arguments=arguments,
                                    truncated=truncated,
                                    outcome_unknown=outcome_unknown,
                                )
                            )
                        tool_call_sources.setdefault(call_id, []).append(
                            (sequence, call_index)
                        )
        elif message_type == "tool" and "tool_result" in include:
            call_id = data.get("tool_call_id")
            if (
                tool_call_ids is not None
                and (not isinstance(call_id, str) or call_id not in tool_call_ids)
            ):
                continue
            result, truncated = _bounded(data.get("content"))
            if not result:
                continue
            sources = (
                tool_call_sources.get(call_id)
                if isinstance(call_id, str)
                else None
            )
            call_source = sources.pop() if sources else None
            assistant_sequence = call_source[0] if call_source else None
            call_index = call_source[1] if call_source else None
            parts.append(
                TurnResponsePartDTO(
                    part_id=(
                        f"tool-call:{assistant_sequence}:{call_index}"
                        if assistant_sequence is not None and call_index is not None
                        else f"tool-result:{sequence}"
                    ),
                    kind="tool_result",
                    projection="detail",
                    status=(
                        "failed"
                        if data.get("status") in {"failed", "error"}
                        else "completed"
                    ),
                    source=TurnResponseSourceDTO(
                        message_sequence=sequence,
                        assistant_message_sequence=assistant_sequence,
                        call_index=call_index,
                        result_message_sequence=sequence,
                    ),
                    tool_call_id=call_id if isinstance(call_id, str) else None,
                    result=result,
                    text=result,
                    truncated=truncated,
                )
            )
    if "tool_summary" in include:
        existing_tool_kinds = {
            part.kind for part in parts if part.kind in {"tool_call", "tool_result"}
        }
        parts.extend(
            part
            for part in _summary_tool_parts(
                projection or {},
                limit=65536,
                include=include,
            )
            if part.kind not in existing_tool_kinds
        )
        parts.sort(
            key=lambda part: (
                part.source.message_sequence,
                part.source.content_block_index
                if part.source.content_block_index is not None
                else 1_000_000_000,
                part.source.item_index
                if part.source.item_index is not None
                else 1_000_000_000,
                part.source.call_index
                if part.source.call_index is not None
                else 1_000_000_000,
                1 if part.kind == "tool_result" else 0,
            )
        )
    return parts[:max_parts]
