"""把 canonical rollout 消息映射为历史和 live 共用的响应部件。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Literal

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


def _response_status(value: object) -> str:
    if value in {"completed", "success", "succeeded", "ok"}:
        return "completed"
    if value in {"failed", "error", "unknown"}:
        return "failed"
    if value in {"open", "pending"}:
        return "pending"
    if value in {"active", "running"}:
        return "running"
    if value in {"partial", "incomplete", "cancelled"}:
        return "cancelled"
    raise ValueError(f"未知 canonical item status: {value!r}")


def _activity_parts_from_projection(
    projection: Mapping[str, object],
    *,
    mode: Projection,
    include: frozenset[str],
) -> list[TurnResponsePartDTO]:
    """按后端 canonical identity/order 投影中间 item，不在 mapper 去重或排序。"""
    raw_items = projection.get("activity_items")
    if not isinstance(raw_items, list):
        raise TypeError("Turn projection 缺少 activity_items")
    parts: list[TurnResponsePartDTO] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise TypeError("Turn activity item 必须是对象")
        raw_kind = raw.get("kind")
        kind = (
            "reasoning_summary"
            if raw_kind == "compaction_summary"
            else raw_kind
        )
        if kind not in {
            "reasoning",
            "reasoning_summary",
            "reasoning_encrypted",
            "tool_call",
            "tool_result",
        }:
            raise ValueError(f"未知 Turn activity item kind: {raw_kind!r}")
        if kind == "reasoning" and not include & {"thinking", "reasoning_detail"}:
            continue
        if kind == "reasoning_summary" and not include & {
            "thinking",
            "reasoning_summary",
            "reasoning_detail",
        }:
            continue
        if kind == "reasoning_encrypted" and "encrypted_reasoning_meta" not in include:
            continue
        if kind == "tool_call" and not include & {"tool_summary", "tool_call"}:
            continue
        if kind == "tool_result" and not include & {"tool_summary", "tool_result"}:
            continue
        item_id = raw.get("item_id")
        item_sequence = raw.get("item_sequence")
        part_ordinal = raw.get("part_ordinal", 0)
        created_at = raw.get("created_at")
        if (
            not isinstance(item_id, str)
            or not item_id
            or not isinstance(item_sequence, int)
            or isinstance(item_sequence, bool)
            or item_sequence < 1
            or not isinstance(part_ordinal, int)
            or isinstance(part_ordinal, bool)
            or part_ordinal < 0
            or not isinstance(created_at, str)
            or not created_at
        ):
            raise TypeError("Turn activity item 缺少 canonical identity/order/time")
        message_sequence = raw.get("message_sequence", 0)
        if not isinstance(message_sequence, int) or isinstance(message_sequence, bool):
            raise TypeError("Turn activity item message_sequence 非法")
        text = raw.get("text") if isinstance(raw.get("text"), str) else ""
        tool_call_id = raw.get("tool_call_id")
        tool_name = raw.get("tool_name")
        parts.append(
            TurnResponsePartDTO(
                part_id=f"{item_id}:part:{part_ordinal}",
                kind=kind,
                projection=mode,
                status=_response_status(raw.get("status")),
                source=TurnResponseSourceDTO(
                    message_sequence=message_sequence,
                    assistant_message_sequence=(
                        raw.get("assistant_message_sequence")
                        if isinstance(raw.get("assistant_message_sequence"), int)
                        else None
                    ),
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
                    call_index=(
                        raw.get("call_index")
                        if isinstance(raw.get("call_index"), int)
                        else None
                    ),
                    result_message_sequence=(
                        raw.get("result_message_sequence")
                        if isinstance(raw.get("result_message_sequence"), int)
                        else None
                    ),
                    item_id=item_id,
                    item_sequence=item_sequence,
                    part_ordinal=part_ordinal,
                    created_at=created_at,
                    elapsed_ms=(
                        raw.get("elapsed_ms")
                        if isinstance(raw.get("elapsed_ms"), int)
                        else None
                    ),
                ),
                text=text,
                carrier_type=(
                    "compaction_summary"
                    if raw_kind == "compaction_summary"
                    else raw.get("carrier_type")
                    if isinstance(raw.get("carrier_type"), str)
                    else None
                ),
                tool_call_id=(
                    tool_call_id if isinstance(tool_call_id, str) else None
                ),
                tool_name=tool_name if isinstance(tool_name, str) else None,
                truncated=raw.get("truncated") is True,
                partial=raw.get("status") in {"partial", "incomplete"},
            )
        )
    return parts


def _tool_payloads(
    records: Sequence[Mapping[str, object]],
) -> tuple[
    dict[tuple[str, int, int], tuple[str, bool]],
    dict[tuple[str, int], tuple[str, bool]],
]:
    """读取详情正文，但不据此决定 Item identity、数量或顺序。"""
    calls: dict[tuple[str, int, int], tuple[str, bool]] = {}
    results: dict[tuple[str, int], tuple[str, bool]] = {}
    for record in records:
        sequence = record.get("_indexed_sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise TypeError("rollout message 缺少有效 message_sequence")
        message = _serialized_message(record)
        data = _message_data(message)
        if message.get("type") == "ai":
            raw_calls = data.get("tool_calls")
            if not isinstance(raw_calls, list):
                continue
            for call_index, call in enumerate(raw_calls):
                if not isinstance(call, Mapping):
                    continue
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    continue
                calls[(call_id, sequence, call_index)] = _bounded(
                    json.dumps(call.get("args", {}), ensure_ascii=False, default=str)
                )
        elif message.get("type") == "tool":
            call_id = data.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                results[(call_id, sequence)] = _bounded(data.get("content"))
    return calls, results


def _enrich_activity_parts(
    parts: list[TurnResponsePartDTO],
    records: Sequence[Mapping[str, object]],
    *,
    projection: Mapping[str, object],
) -> list[TurnResponsePartDTO]:
    """用定点加载的 JSONL payload 丰富 canonical part，保持后端原顺序。"""
    calls, results = _tool_payloads(records)
    result_call_ids = {
        part.tool_call_id
        for part in parts
        if part.kind == "tool_result" and part.tool_call_id is not None
    }
    terminal_turn = projection.get("status") in _TERMINAL_TURN_STATUSES
    enriched: list[TurnResponsePartDTO] = []
    for part in parts:
        source = part.source
        if part.kind == "tool_call" and part.tool_call_id is not None:
            assistant_sequence = source.assistant_message_sequence
            call_index = source.call_index
            payload = (
                calls.get((part.tool_call_id, assistant_sequence, call_index))
                if assistant_sequence is not None and call_index is not None
                else None
            )
            outcome_unknown = terminal_turn and part.tool_call_id not in result_call_ids
            enriched.append(
                part.model_copy(
                    update={
                        "status": "failed" if outcome_unknown else part.status,
                        "arguments": payload[0] if payload is not None else None,
                        "truncated": part.truncated
                        or (payload[1] if payload is not None else False),
                        "outcome_unknown": outcome_unknown,
                    }
                )
            )
            continue
        if part.kind == "tool_result" and part.tool_call_id is not None:
            result_sequence = source.result_message_sequence
            payload = (
                results.get((part.tool_call_id, result_sequence))
                if result_sequence is not None
                else None
            )
            enriched.append(
                part.model_copy(
                    update={
                        "result": payload[0] if payload is not None else None,
                        "text": payload[0] if payload is not None else part.text,
                        "truncated": part.truncated
                        or (payload[1] if payload is not None else False),
                    }
                )
            )
            continue
        enriched.append(part)
    return enriched


def _final_response_part(
    records: Sequence[Mapping[str, object]],
    projection: Mapping[str, object],
    *,
    mode: Projection,
    include: frozenset[str],
) -> TurnResponsePartDTO | None:
    if not ({"final_response", "assistant"} & include):
        return None
    final_sequence = projection.get("final_message_sequence")
    final_text = projection.get("final_response_text")
    if (
        not isinstance(final_sequence, int)
        or isinstance(final_sequence, bool)
        or not isinstance(final_text, str)
        or not final_text
    ):
        return None
    final_item_id = projection.get("final_item_id")
    final_item_sequence = projection.get("final_item_sequence")
    final_item_created_at = projection.get("final_item_created_at")
    if (
        not isinstance(final_item_id, str)
        or not final_item_id
        or not isinstance(final_item_sequence, int)
        or isinstance(final_item_sequence, bool)
        or not isinstance(final_item_created_at, str)
        or not final_item_created_at
    ):
        raise RuntimeError("final response projection 缺少 canonical identity")
    completion_reason, partial = _completion_metadata_for_sequence(
        records, final_sequence
    )
    return TurnResponsePartDTO(
        part_id=f"{final_item_id}:final",
        kind="text" if partial else "final_text",
        projection=mode,
        source=TurnResponseSourceDTO(
            message_sequence=final_sequence,
            item_id=final_item_id,
            item_sequence=final_item_sequence,
            part_ordinal=1_000_000_000,
            created_at=final_item_created_at,
            elapsed_ms=0,
        ),
        text=final_text[:65536],
        truncated=bool(projection.get("final_response_text_truncated")),
        final=not partial,
        completion_reason=completion_reason,
        partial=partial,
    )


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
    if projection is None:
        raise RuntimeError("历史 response parts 缺少 canonical Turn projection")
    parts = _activity_parts_from_projection(
        projection,
        mode=mode,
        include=include,
    )
    if tool_call_ids is not None:
        parts = [
            part
            for part in parts
            if part.kind not in {"tool_call", "tool_result"}
            or part.tool_call_id in tool_call_ids
        ]
    if mode == "detail":
        parts = _enrich_activity_parts(parts, records, projection=projection)
    final_part = _final_response_part(
        records,
        projection,
        mode=mode,
        include=include,
    )
    if final_part is not None:
        parts.append(final_part)
    return parts[:max_parts]
