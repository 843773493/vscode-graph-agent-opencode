"""把 canonical rollout 消息映射为历史和 live 共用的响应部件。"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from heapq import nsmallest
from typing import Literal

from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import (
    matches_tool_call_id,
    provider_tool_call_id,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.schemas.internal_v2.turn import TurnResponsePartDTO, TurnResponseSourceDTO
from app.services.mapping.itemized.content_blocks import (
    direct_content_blocks,
    reasoning_content_rows,
)
from app.services.mapping.itemized.provider_history import visible_text

#: 文本部件正文上限；canonical 投影按此截断，超限只置 truncated 供前端补拉。
_TEXT_LIMIT = 65536

#: 最终回答不属于 activity item，用超大 part_ordinal 排在所有 activity 部件之后。
_FINAL_PART_ORDINAL = 1_000_000_000

#: activity item kind → 使该部件正文可见的 include 闭集；未登记的 kind 必须显式失败。
_INCLUDE_BY_KIND: dict[str, frozenset[str]] = {
    "text": frozenset({"text", "assistant_text", "assistant", "final_response"}),
    "reasoning": frozenset({"thinking", "reasoning_detail"}),
    "reasoning_summary": frozenset(
        {"thinking", "reasoning_summary", "reasoning_detail"}
    ),
    "reasoning_encrypted": frozenset({"encrypted_reasoning_meta"}),
    "compaction_summary": frozenset({"text", "thinking", "reasoning_summary", "reasoning_detail"}),
    "tool_call": frozenset({"tool_summary", "tool_call"}),
    "tool_result": frozenset({"tool_summary", "tool_result"}),
}

Projection = Literal["summary", "detail", "streaming"]

_TERMINAL_TURN_STATUSES = {
    "completed",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
}


def _text_chunks(value: object) -> Iterator[str]:
    if isinstance(value, str):
        if value:
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from _text_chunks(item)
    elif isinstance(value, Mapping):
        candidate = value.get("text")
        if isinstance(candidate, str) and candidate:
            yield candidate


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


def _bounded(value: object, limit: int = _TEXT_LIMIT) -> tuple[str, bool]:
    if limit < 0:
        text = "".join(_text_chunks(value))
        return text[:limit], len(text) > limit

    text_chunks: list[str] = []
    text_length = 0
    truncated = False
    for chunk in _text_chunks(value):
        remaining = limit - text_length
        if remaining == 0:
            truncated = True
            break
        if len(chunk) > remaining:
            text_chunks.append(chunk[:remaining])
            truncated = True
            break
        text_chunks.append(chunk)
        text_length += len(chunk)
    if len(text_chunks) == 1:
        return text_chunks[0], truncated
    return "".join(text_chunks), truncated


def _int_or_none(raw: Mapping[str, object], key: str) -> int | None:
    """读取可选 canonical 坐标；缺失或非 int 都返回 None（与原内联分支逐字等价）。"""
    value = raw.get(key)
    return value if isinstance(value, int) else None


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


def response_part_order(part: TurnResponsePartDTO) -> tuple[int, int]:
    """所有 history lane 共用 membership 与部件坐标，不使用物理序号排序。"""
    logical = part.source.logical_item_ordinal
    ordinal = part.source.part_ordinal
    if logical is None or ordinal is None:
        raise RuntimeError(f"response part 缺少 membership ordinal 或 part ordinal: {part.part_id}")
    return logical, ordinal


def _part_projection(kind: str, *, mode: Projection, include: frozenset[str]) -> Projection:
    """部件的投影级别必须反映自身 payload 是否真的已加载。

    ``mode`` 是整个请求的投影级别；当 include 含 ``thinking`` 等字段时，即使
    没有请求 ``tool_call``/``tool_result``，请求也会被判定为 detail。此时工具
    部件并没有参数正文，若仍标记为 detail，前端会误以为详情已加载而不再补拉，
    最终只显示“输入参数”标题却没有内容。因此工具部件按各自的 include 判定。
    """
    if kind == "tool_call":
        return "detail" if "tool_call" in include else "summary"
    if kind == "tool_result":
        return "detail" if "tool_result" in include else "summary"
    return mode


def _activity_parts_from_projection(
    projection: Mapping[str, object],
    *,
    mode: Projection,
    include: frozenset[str],
    tool_call_ids: frozenset[str] | None,
) -> list[TurnResponsePartDTO]:
    """保留后端 canonical identity 与当前 view 的成员坐标。"""
    raw_items = projection.get("activity_items")
    if not isinstance(raw_items, list):
        raise TypeError("Turn projection 缺少 activity_items")
    parts: list[TurnResponsePartDTO] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise TypeError("Turn activity item 必须是对象")
        raw_kind = raw.get("kind")
        kind = raw_kind
        include_fields = _INCLUDE_BY_KIND.get(kind) if isinstance(kind, str) else None
        if include_fields is None:
            raise ValueError(f"未知 Turn activity item kind: {raw_kind!r}")
        if not include & include_fields:
            continue
        item_id = raw.get("item_id")
        item_sequence = raw.get("item_sequence")
        logical_item_ordinal = raw.get("logical_item_ordinal")
        part_ordinal = raw.get("part_ordinal", 0)
        created_at = raw.get("created_at")
        if (
            not isinstance(item_id, str)
            or not item_id
            or not isinstance(item_sequence, int)
            or isinstance(item_sequence, bool)
            or item_sequence < 1
            or not isinstance(logical_item_ordinal, int)
            or isinstance(logical_item_ordinal, bool)
            or logical_item_ordinal < 0
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
        part_projection = _part_projection(kind, mode=mode, include=include)
        # 工具部件在 summary 投影不携带正文：tool_summary 只提供身份与状态，
        # 完整参数/结果必须经显式 tool_call/tool_result include 补拉。这与
        # summary 模式「不物化工具 payload」的既有契约一致；否则默认历史页
        # 会把 item_projections 的整段工具正文（上限 64KB）随页带出。
        part_text = (
            ""
            if part_projection == "summary" and kind in {"tool_call", "tool_result"}
            else text
        )
        tool_call_id = raw.get("tool_call_id")
        identity = ItemProjectionIdentity.from_dict(
            {"model_call_id": raw.get("model_call_id")}
        )
        if (
            tool_call_ids is not None
            and kind in {"tool_call", "tool_result"}
            and (
                not isinstance(tool_call_id, str)
                or not any(
                    matches_tool_call_id(identity, tool_call_id, selected_id)
                    for selected_id in tool_call_ids
                )
            )
        ):
            continue
        tool_name = raw.get("tool_name")
        parts.append(
            TurnResponsePartDTO(
                part_id=f"{item_id}:part:{part_ordinal}",
                kind=kind,
                projection=part_projection,
                status=_response_status(raw.get("status")),
                source=TurnResponseSourceDTO(
                    message_sequence=message_sequence,
                    assistant_message_sequence=_int_or_none(
                        raw, "assistant_message_sequence"
                    ),
                    content_block_index=_int_or_none(raw, "content_block_index"),
                    item_index=_int_or_none(raw, "item_index"),
                    call_index=_int_or_none(raw, "call_index"),
                    result_message_sequence=_int_or_none(
                        raw, "result_message_sequence"
                    ),
                    item_id=item_id,
                    item_sequence=item_sequence,
                    logical_item_ordinal=logical_item_ordinal,
                    part_ordinal=part_ordinal,
                    created_at=created_at,
                    elapsed_ms=_int_or_none(raw, "elapsed_ms"),
                ),
                text=part_text,
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
                completion_reason=(
                    raw.get("completion_reason")
                    if kind == "text" and isinstance(raw.get("completion_reason"), str)
                    else None
                ),
                truncated=raw.get("truncated") is True,
                partial=raw.get("status") in {"partial", "incomplete"},
            )
        )
    return parts


def _tool_payloads(
    records: Sequence[Mapping[str, object]],
) -> tuple[
    dict[tuple[str | None, str, int, int], tuple[str, bool]],
    dict[tuple[str | None, str], tuple[str, bool] | None],
    dict[tuple[str | None, str, int], tuple[str, bool]],
]:
    """读取同一 typed 模型调用内的详情正文，不据此决定 Item identity 或顺序。"""
    calls: dict[tuple[str | None, str, int, int], tuple[str, bool]] = {}
    calls_by_identity: dict[tuple[str | None, str], tuple[str, bool] | None] = {}
    results: dict[tuple[str | None, str, int], tuple[str, bool]] = {}
    for record in records:
        sequence = record.get("_indexed_sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise TypeError("rollout message 缺少有效 message_sequence")
        message = _serialized_message(record)
        data = _message_data(message)
        metadata = data.get("response_metadata")
        identity = ItemProjectionIdentity.from_dict(
            {"model_call_id": metadata.get("model_call_id") if isinstance(metadata, Mapping) else None}
        )
        scope = identity.model_call_id
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
                call_id = provider_tool_call_id(identity, call_id)
                payload = _bounded(
                    json.dumps(call.get("args", {}), ensure_ascii=False, default=str)
                )
                calls[(scope, call_id, sequence, call_index)] = payload
                key = (scope, call_id)
                # 同一 scope 内复用 raw ID 时，没有坐标的部件不能猜测归属。
                calls_by_identity[key] = None if key in calls_by_identity else payload
        elif message.get("type") == "tool":
            call_id = data.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                results[(scope, provider_tool_call_id(identity, call_id), sequence)] = _bounded(
                    data.get("content")
                )
    return calls, calls_by_identity, results


def _enrich_activity_parts(
    parts: list[TurnResponsePartDTO],
    records: Sequence[Mapping[str, object]],
    *,
    projection: Mapping[str, object],
) -> list[TurnResponsePartDTO]:
    """用同 snapshot 的 typed scope 和显式坐标补充 canonical payload。"""
    if not any(
        part.kind in {"tool_call", "tool_result"} and part.tool_call_id is not None
        for part in parts
    ):
        return parts

    calls, calls_by_identity, results = _tool_payloads(records)
    identities = {
        raw["item_id"]: ItemProjectionIdentity.from_dict(
            {"model_call_id": raw.get("model_call_id")}
        )
        for raw in projection["activity_items"]
    }
    result_call_ids = {
        (
            identities[part.source.item_id].model_call_id,
            provider_tool_call_id(identities[part.source.item_id], part.tool_call_id),
        )
        for part in parts
        if part.kind == "tool_result" and part.tool_call_id is not None
    }
    terminal_turn = projection.get("status") in _TERMINAL_TURN_STATUSES
    enriched: list[TurnResponsePartDTO] = []
    for part in parts:
        source = part.source
        if part.kind not in {"tool_call", "tool_result"} or part.tool_call_id is None:
            enriched.append(part)
            continue
        identity = identities[source.item_id]
        key = (identity.model_call_id, provider_tool_call_id(identity, part.tool_call_id))
        if part.kind == "tool_call":
            assistant_sequence = source.assistant_message_sequence
            call_index = source.call_index
            payload = (
                calls.get((*key, assistant_sequence, call_index))
                if assistant_sequence is not None and call_index is not None
                else calls_by_identity.get(key)
            )
            outcome_unknown = terminal_turn and key not in result_call_ids
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
        result_sequence = source.result_message_sequence
        payload = results.get((*key, result_sequence)) if result_sequence is not None else None
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
    final_logical_item_ordinal = projection.get("final_logical_item_ordinal")
    # final 沿 canonical final item 的创建时刻；固定 elapsed_ms=0，不计相邻 activity 差。
    final_item_created_at = projection.get("final_item_created_at")
    if (
        not isinstance(final_item_id, str)
        or not final_item_id
        or not isinstance(final_item_sequence, int)
        or isinstance(final_item_sequence, bool)
        or not isinstance(final_logical_item_ordinal, int)
        or isinstance(final_logical_item_ordinal, bool)
        or final_logical_item_ordinal < 0
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
            logical_item_ordinal=final_logical_item_ordinal,
            part_ordinal=_FINAL_PART_ORDINAL,
            created_at=final_item_created_at,
            elapsed_ms=0,
        ),
        text=final_text[:_TEXT_LIMIT],
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
        tool_call_ids=tool_call_ids,
    )
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
    if max_parts < 0:
        return sorted(parts, key=response_part_order)[:max_parts]
    return nsmallest(max_parts, parts, key=response_part_order)


def _internal_content_parts(
    item: CanonicalItemRecord,
    *,
    include: frozenset[str],
    source: Mapping[str, object],
    projection: Projection,
) -> list[TurnResponsePartDTO]:
    """从 canonical content 展开可见语义；密文只投影存在性。"""
    rows: list[tuple[int, int | None, str, str, str | None]] = []
    for block_index, block in enumerate(direct_content_blocks(item.payload)):
        if block.get("type") in {"text", "input_text", "output_text", "refusal"}:
            rows.append((block_index, None, "text", visible_text([block]), None))
    for row in reasoning_content_rows(item.payload):
        for kind, text in (("reasoning", row.reasoning_text), ("reasoning_summary", row.summary_text)):
            if isinstance(text, str) and text:
                rows.append((row.content_block_index, row.item_index, kind, text, row.carrier_type))
        if row.encrypted:
            rows.append((row.content_block_index, row.item_index, "reasoning_encrypted", "", row.carrier_type))
    rows.sort(key=lambda row: (row[0], row[1] if row[1] is not None else 0))
    identity = item.projection_identity
    parts: list[TurnResponsePartDTO] = []
    # 先分配完整 content 的 ordinal，再应用 include，补拉详情不会改变 part 身份。
    for ordinal, (block_index, item_index, kind, value, carrier_type) in enumerate(rows):
        if not include & _INCLUDE_BY_KIND[kind]:
            continue
        text, truncated = _bounded(value)
        parts.append(TurnResponsePartDTO(
            part_id=f"{item.item_id}:part:{ordinal}",
            kind=kind,
            projection=projection,
            status=_response_status(item.status),
            source=TurnResponseSourceDTO(**{
                **source,
                "content_block_index": block_index,
                "item_index": item_index,
                "part_ordinal": ordinal,
            }),
            text=text,
            carrier_type=carrier_type,
            truncated=truncated,
            completion_reason=identity.completion_reason if identity else None,
            partial=item.status in {"partial", "incomplete"},
        ))
    return parts


def response_parts_from_internal_item(
    item: CanonicalItemRecord,
    *,
    include: frozenset[str],
    logical_item_ordinal: int,
    elapsed_ms: int,
) -> list[TurnResponsePartDTO]:
    """映射 standalone item，沿原创建时刻和 reader 提供的逻辑前项整数毫秒差。

    reader 在同 view、同 execution 的 visible/display_only 成员中找前项，
    首项从 execution.created_at 起算；这里保留该 elapsed_ms，不按正文或分页重算。
    """
    identity = item.projection_identity
    semantic_kind = item.semantic_kind
    payload = item.payload
    kind: str | None = None
    allowed_include: frozenset[str] = frozenset()
    if semantic_kind == "assistant_output":
        kind = "text"
        allowed_include = _INCLUDE_BY_KIND[kind]
    elif semantic_kind == "reasoning":
        kind = (
            "reasoning_summary"
            if item.payload_kind == "summary"
            else "reasoning_encrypted"
            if item.payload_kind in {"opaque", "extension"}
            else "reasoning"
        )
        allowed_include = _INCLUDE_BY_KIND[kind]
    elif semantic_kind == "compaction_summary":
        kind = "compaction_summary"
        allowed_include = _INCLUDE_BY_KIND[kind]
    elif semantic_kind == "tool_call":
        kind = "tool_call"
        allowed_include = _INCLUDE_BY_KIND[kind]
    elif semantic_kind == "tool_result":
        kind = "tool_result"
        allowed_include = _INCLUDE_BY_KIND[kind]
    structured_content = semantic_kind == "assistant_output" and item.payload_kind == "structured_content"
    if kind is None or (not structured_content and not include & allowed_include):
        return []

    part_ordinal = identity.block_index if identity is not None and identity.block_index is not None else 0
    message_sequence = 0
    common_source = {
        "message_sequence": message_sequence,
        "assistant_message_sequence": None,
        "content_block_index": identity.block_index if identity else None,
        "item_index": None,
        "item_id": item.item_id,
        "item_sequence": item.item_sequence,
        "logical_item_ordinal": logical_item_ordinal,
        "elapsed_ms": elapsed_ms,
        "created_at": item.created_at,
    }
    projection: Projection = (
        "detail"
        if include
        & {
            "text",
            "assistant_text",
            "assistant",
            "thinking",
            "reasoning_detail",
            "tool_call",
            "tool_result",
        }
        else "summary"
    )
    if structured_content:
        return _internal_content_parts(item, include=include, source=common_source, projection=projection)
    if kind == "reasoning_encrypted":
        return [
            TurnResponsePartDTO(
                part_id=f"{item.item_id}:part:{part_ordinal}",
                kind=kind,
                projection=projection,
                status=_response_status(item.status),
                source=TurnResponseSourceDTO(
                    **common_source,
                    part_ordinal=part_ordinal,
                ),
            )
        ]

    if kind == "tool_call":
        raw_calls = payload.get("tool_calls") if isinstance(payload, Mapping) else None
        calls = raw_calls if isinstance(raw_calls, list) else [payload]
        parts: list[TurnResponsePartDTO] = []
        for call_index, raw_call in enumerate(calls):
            if not isinstance(raw_call, Mapping):
                raise TypeError(f"canonical tool_call payload 非法: {item.item_id}")
            raw_id = raw_call.get("id", raw_call.get("tool_call_id"))
            tool_call_id = (
                raw_id if isinstance(raw_id, str) and raw_id else None
            )
            tool_name = raw_call.get("name")
            tool_name = tool_name if isinstance(tool_name, str) else None
            raw_arguments = raw_call.get("args", raw_call.get("arguments"))
            arguments = (
                raw_arguments
                if isinstance(raw_arguments, str)
                else json.dumps(raw_arguments, ensure_ascii=False, separators=(",", ":"))
                if raw_arguments is not None
                else None
            )
            ordinal = part_ordinal + call_index
            parts.append(
                TurnResponsePartDTO(
                    part_id=f"{item.item_id}:part:{ordinal}",
                    kind=kind,
                    projection=("detail" if "tool_call" in include else "summary"),
                    status=_response_status(item.status),
                    source=TurnResponseSourceDTO(
                        **common_source,
                        call_index=call_index,
                        part_ordinal=ordinal,
                    ),
                    text=tool_name or "",
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments if "tool_call" in include else None,
                )
            )
        return parts

    if kind == "tool_result":
        tool_call_id = item.tool_result_evidence.tool_call_id if item.tool_result_evidence is not None else payload["tool_call_id"]
        tool_name = payload.get("name") if isinstance(payload, Mapping) else None
        raw_result = payload.get("content") if isinstance(payload, Mapping) else payload
        result, truncated = _bounded(raw_result)
        detailed = "tool_result" in include
        ordinal = part_ordinal
        return [
            TurnResponsePartDTO(
                part_id=f"{item.item_id}:part:{ordinal}",
                kind=kind,
                projection=("detail" if detailed else "summary"),
                status=_response_status(item.status),
                source=TurnResponseSourceDTO(
                    **common_source,
                    part_ordinal=ordinal,
                ),
                text=result if detailed else "",
                tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
                tool_name=tool_name if isinstance(tool_name, str) else None,
                result=result if detailed else None,
                truncated=truncated,
            )
        ]

    if kind in {"reasoning_summary", "compaction_summary"} and isinstance(payload, Mapping):
        text_value = payload.get("summary", payload.get("text", ""))
    else:
        text_value = payload
    text, truncated = _bounded(text_value)
    ordinal = part_ordinal
    return [
        TurnResponsePartDTO(
            part_id=f"{item.item_id}:part:{ordinal}",
            kind=kind,
            projection=projection,
            status=_response_status(item.status),
            source=TurnResponseSourceDTO(
                **common_source,
                part_ordinal=ordinal,
            ),
            text=text,
            carrier_type="compaction_summary" if kind == "compaction_summary" else None,
            truncated=truncated,
            completion_reason=identity.completion_reason if identity else None,
            partial=item.status in {"partial", "incomplete"},
        )
    ]
