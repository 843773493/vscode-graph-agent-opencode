"""把 canonical rollout 消息映射为历史和 live 共用的响应 Item。"""

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
from app.schemas.internal_v2.turn import (
    ToolExecutionSummaryDTO,
    TurnResponseItemDTO,
    TurnResponseSourceDTO,
)
from app.services.mapping.itemized.content_blocks import (
    reasoning_content_rows,
)
from app.services.mapping.itemized.provider_history import (
    canonical_reasoning_projection,
    visible_text,
)

#: 文本 Item正文上限；canonical 投影按此截断，超限只置 truncated 供前端补拉。
_TEXT_LIMIT = 65536

#: activity item kind → 使该部件正文可见的 include 闭集；未登记的 kind 必须显式失败。
_INCLUDE_BY_KIND: dict[str, frozenset[str]] = {
    "text": frozenset({"text", "assistant_text", "assistant", "final_response"}),
    "content": frozenset({"text", "assistant_text", "assistant", "final_response"}),
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


def _bounded(value: object) -> tuple[str, bool]:
    text_chunks: list[str] = []
    text_length = 0
    truncated = False
    for chunk in _text_chunks(value):
        remaining = _TEXT_LIMIT - text_length
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


def response_item_order(part: TurnResponseItemDTO) -> int:
    """所有 history lane 只使用当前 view 的逻辑 Item 顺序。"""
    logical = part.source.logical_item_ordinal
    if logical is None:
        raise RuntimeError(f"response Item 缺少 membership ordinal: {part.id}")
    return logical


def _item_projection(kind: str, *, mode: Projection, include: frozenset[str]) -> Projection:
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
    if kind == "content":
        # 摘要仅携带类型；结构化正文由原 Item 定点读取入口显式加载。
        return "summary"
    return mode


def activity_items_from_projection(
    projection: Mapping[str, object],
    *,
    mode: Projection,
    include: frozenset[str],
    tool_call_ids: frozenset[str] | None,
) -> list[TurnResponseItemDTO]:
    """保留后端 canonical identity 与当前 view 的成员坐标。"""
    raw_items = projection.get("activity_items")
    if not isinstance(raw_items, list):
        raise TypeError("Turn projection 缺少 activity_items")
    parts: list[TurnResponseItemDTO] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise TypeError("Turn activity item 必须是对象")
        raw_kind = raw.get("kind")
        kind = raw_kind
        include_fields = _INCLUDE_BY_KIND.get(kind) if isinstance(kind, str) else None
        if include_fields is None:
            raise ValueError(f"未知 Turn activity item kind: {raw_kind!r}")
        if "activity_outline" not in include and not include & include_fields:
            continue
        item_id = raw.get("item_id")
        item_sequence = raw.get("item_sequence")
        logical_item_ordinal = raw.get("logical_item_ordinal")
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
            or not isinstance(created_at, str)
            or not created_at
        ):
            raise TypeError("Turn activity item 缺少 canonical identity/order/time")
        message_sequence = raw.get("message_sequence", 0)
        if not isinstance(message_sequence, int) or isinstance(message_sequence, bool):
            raise TypeError("Turn activity item message_sequence 非法")
        text = raw.get("text") if isinstance(raw.get("text"), str) else ""
        part_projection = "summary" if "activity_outline" in include else _item_projection(kind, mode=mode, include=include)
        # 工具部件在 summary 投影不携带正文：tool_summary 只提供身份与状态，
        # 完整参数/结果必须经显式 tool_call/tool_result include 补拉。这与
        # summary 模式「不物化工具 payload」的既有契约一致；否则默认历史页
        # 会把 item_projections 的整段工具正文（上限 64KB）随页带出。
        part_text = (
            ""
            if part_projection == "summary" and (kind in {"tool_call", "tool_result"} or "activity_outline" in include and kind != "text")
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
            TurnResponseItemDTO(
                id=item_id,
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
                    model_call_id=identity.model_call_id,
                    message_group=raw.get("message_group"),
                    item_sequence=item_sequence,
                    logical_item_ordinal=logical_item_ordinal,
                    created_at=created_at,
                    elapsed_ms=_int_or_none(raw, "elapsed_ms"),
                    process_segment_item_id=raw.get("process_segment_item_id"),
                    activity_group_item_id=raw.get("activity_group_item_id"),
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
                tool_execution=(ToolExecutionSummaryDTO.model_validate(raw["tool_execution"])
                                if raw.get("tool_execution") is not None else None),
                tool_outcome=raw.get("tool_outcome"),
                outcome_unknown=raw.get("tool_outcome") == "unknown",
                completion_reason=(
                    raw.get("completion_reason")
                    if kind in {"text", "content"} and isinstance(raw.get("completion_reason"), str)
                    else None
                ),
                truncated=raw.get("truncated") is True,
                partial=raw.get("status") in {"partial", "incomplete"},
            )
        )
    return parts


def _tool_payloads(
    records: Sequence[Mapping[str, object]],
    *,
    kinds: set[str],
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
        if message.get("type") == "ai" and "tool_call" in kinds:
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
        elif message.get("type") == "tool" and "tool_result" in kinds:
            call_id = data.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                results[(scope, provider_tool_call_id(identity, call_id), sequence)] = _bounded(
                    data.get("content")
                )
    return calls, calls_by_identity, results


def _enrich_activity_items(
    parts: list[TurnResponseItemDTO],
    records: Sequence[Mapping[str, object]],
    *,
    projection: Mapping[str, object],
) -> list[TurnResponseItemDTO]:
    """用同 snapshot 的 typed scope 和显式坐标补充 canonical payload。"""
    calls, calls_by_identity, results = _tool_payloads(
        records, kinds={part.kind for part in parts if part.projection == "detail"},
    )
    reasoning = {}
    for record in records:
        for row in reasoning_content_rows(_message_data(_serialized_message(record)).get("content")):
            for kind, text in (("reasoning", row.reasoning_text), ("reasoning_summary", row.summary_text)):
                if text:
                    reasoning[(record.get("_indexed_sequence"), kind, row.content_block_index, row.item_index)] = _bounded(text)
    identities = {
        raw["item_id"]: ItemProjectionIdentity.from_dict(
            {"model_call_id": raw.get("model_call_id")}
        )
        for raw in projection["activity_items"]
    }
    enriched: list[TurnResponseItemDTO] = []
    for part in parts:
        if part.projection != "detail":
            enriched.append(part)
            continue
        source = part.source
        if part.kind not in {"tool_call", "tool_result"} or part.tool_call_id is None:
            payload = reasoning.get((source.message_sequence, part.kind, source.content_block_index, source.item_index))
            enriched.append(part.model_copy(update={"text":payload[0], "truncated":part.truncated or payload[1]}) if payload is not None else part)
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
            # 详情只补正文；结果与执行状态由原安全投影交付，不能从本页缺回执推断。
            enriched.append(
                part.model_copy(
                    update={
                        "arguments": payload[0] if payload is not None else None,
                        "truncated": part.truncated
                        or (payload[1] if payload is not None else False),
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


def _final_response_items(
    projection: Mapping[str, object],
    *,
    mode: Projection,
    include: frozenset[str],
) -> list[TurnResponseItemDTO]:
    """最终回答逐个引用原输出成员，不把整组正文挂到末 Item。"""
    if not ({"final_response", "assistant", "activity_outline"} & include):
        return []
    return [part.model_copy(update={"kind": "content" if part.kind == "content" else "final_text", "final": True})
            for part in activity_items_from_projection(
                {"activity_items": projection["final_items"]}, mode=mode,
                include=include, tool_call_ids=None,
            )]


def response_items_from_records(
    records: Sequence[Mapping[str, object]],
    *,
    projection: Mapping[str, object] | None,
    mode: Projection,
    include: frozenset[str],
    tool_call_ids: frozenset[str] | None = None,
    max_items: int = 512,
) -> list[TurnResponseItemDTO]:
    """按 canonical 顺序生成 response Items。

    summary 模式以 SQLite 安全投影为正文及完成语义来源；detail 模式读取
    命中的 JSONL records，不因最终展示再构造聚合内容身份。
    """
    if projection is None:
        raise RuntimeError("历史 response Items 缺少 canonical Turn projection")
    parts = activity_items_from_projection(
        projection,
        mode=mode,
        include=include,
        tool_call_ids=tool_call_ids,
    )
    if mode == "detail":
        parts = _enrich_activity_items(parts, records, projection=projection)
    parts.extend(_final_response_items(
        projection,
        mode=mode,
        include=include,
    ))
    if max_items < 0:
        return sorted(parts, key=response_item_order)[:max_items]
    return nsmallest(max_items, parts, key=response_item_order)


def response_items_from_internal_item(
    item: CanonicalItemRecord,
    *,
    include: frozenset[str],
    logical_item_ordinal: int,
    elapsed_ms: int,
) -> list[TurnResponseItemDTO]:
    """映射 standalone item，沿原创建时刻和 reader 提供的逻辑前项整数毫秒差。

    reader 在同 view、同 execution 的 visible/display_only 成员中找前项，
    首项从 execution.created_at 起算；这里保留该 elapsed_ms，不按正文或分页重算。
    """
    identity = item.projection_identity
    semantic_kind = item.semantic_kind
    payload = item.payload
    reasoning = canonical_reasoning_projection(item) if semantic_kind == "reasoning" else None
    kind: str | None = None
    allowed_include: frozenset[str] = frozenset()
    if semantic_kind == "assistant_output":
        kind = "content" if item.output_block_type is not None else "text"
        allowed_include = _INCLUDE_BY_KIND[kind]
    elif semantic_kind == "reasoning":
        kind = reasoning["kind"]
        if "reasoning_summary" in include and not include & {"thinking", "reasoning_detail"}:
            if not reasoning["summary_text"]:
                return []
            kind = "reasoning_summary"
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
    if kind is None or not include & allowed_include:
        return []

    message_sequence = 0
    common_source = {
        "message_sequence": message_sequence,
        "assistant_message_sequence": None,
        "content_block_index": identity.block_index if identity else None,
        "item_index": None,
        "item_id": item.item_id,
        "model_call_id": identity.model_call_id if identity else None,
        "message_group": {"message_group_id": item.message_group_id,
            "ordinal": item.projection_group.ordinal, "size": item.projection_group.size}
        if item.projection_group is not None else None,
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
    if kind == "reasoning_encrypted":
        return [
            TurnResponseItemDTO(
                id=item.item_id,
                kind=kind,
                projection=projection,
                status=_response_status(item.status),
                source=TurnResponseSourceDTO(
                    **common_source,
                ),
                carrier_type=reasoning["carrier_type"],
                completion_reason=identity.completion_reason if identity else None,
                partial=item.status in {"partial", "incomplete"},
            )
        ]

    if kind == "content":
        content = payload.get("content", payload) if isinstance(payload, Mapping) else payload
        if isinstance(content, (list, tuple)):
            content, = content
        if not isinstance(content, Mapping):
            raise TypeError(f"canonical 非文本输出缺少单个结构化内容块: {item.item_id}")
        # 不截断JSON、媒体URL或base64；超出单项预算时保留身份与类型，明确受限。
        encoded = json.dumps(content, ensure_ascii=False)
        truncated = len(encoded) > _TEXT_LIMIT or len(encoded.encode("utf-8")) > _TEXT_LIMIT
        return [TurnResponseItemDTO(
            id=item.item_id, kind="content", projection="detail",
            status=_response_status(item.status), source=TurnResponseSourceDTO(**common_source),
            carrier_type=item.output_block_type, content=None if truncated else dict(content),
            truncated=truncated, completion_reason=identity.completion_reason if identity else None,
            partial=item.status in {"partial", "incomplete"},
        )]

    if kind == "tool_call":
        raw_calls = payload.get("tool_calls") if isinstance(payload, Mapping) else None
        raw_call, = raw_calls if isinstance(raw_calls, list) else [payload]
        if not isinstance(raw_call, Mapping):
            raise TypeError(f"canonical tool_call payload 非法: {item.item_id}")
        raw_id = raw_call.get("id", raw_call.get("tool_call_id"))
        tool_name = raw_call.get("name")
        raw_arguments = raw_call.get("args", raw_call.get("arguments"))
        arguments = None
        truncated = False
        if "tool_call" in include and raw_arguments is not None:
            arguments, truncated = _bounded(
                raw_arguments if isinstance(raw_arguments, str)
                else json.dumps(raw_arguments, ensure_ascii=False, default=str)
            )
        return [TurnResponseItemDTO(
            id=item.item_id,
            kind=kind,
            projection="detail" if "tool_call" in include else "summary",
            status=_response_status(item.status),
            source=TurnResponseSourceDTO(**common_source),
            text=tool_name if isinstance(tool_name, str) else "",
            tool_call_id=raw_id if isinstance(raw_id, str) and raw_id else None,
            tool_name=tool_name if isinstance(tool_name, str) else None,
            arguments=arguments,
            truncated=truncated,
        )]

    if kind == "tool_result":
        tool_call_id = item.tool_result_evidence.tool_call_id if item.tool_result_evidence is not None else payload["tool_call_id"]
        tool_name = payload.get("name") if isinstance(payload, Mapping) else None
        raw_result = payload.get("content") if isinstance(payload, Mapping) else payload
        result, truncated = _bounded(raw_result)
        detailed = "tool_result" in include
        return [
            TurnResponseItemDTO(
                id=item.item_id,
                kind=kind,
                projection=("detail" if detailed else "summary"),
                status=_response_status(item.status),
                source=TurnResponseSourceDTO(
                    **common_source,
                ),
                text=result if detailed else "",
                tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
                tool_name=tool_name if isinstance(tool_name, str) else None,
                result=result if detailed else None,
                tool_outcome=payload.get("tool_outcome") if isinstance(payload, Mapping) else None,
                truncated=truncated,
            )
        ]

    if semantic_kind == "assistant_output":
        text_value = visible_text(payload)
    elif reasoning is not None:
        text_value = reasoning["summary_text"] if kind == "reasoning_summary" else reasoning["reasoning_text"] or ""
    elif kind in {"reasoning_summary", "compaction_summary"} and isinstance(payload, Mapping):
        text_value = payload.get("summary", payload.get("text", ""))
    else:
        text_value = payload
    text, truncated = _bounded(text_value)
    return [
        TurnResponseItemDTO(
            id=item.item_id,
            kind=kind,
            projection=projection,
            status=_response_status(item.status),
            source=TurnResponseSourceDTO(
                **common_source,
            ),
            text=text,
            carrier_type="compaction_summary" if kind == "compaction_summary" else reasoning["carrier_type"] if reasoning is not None else None,
            truncated=truncated,
            completion_reason=identity.completion_reason if identity else None,
            partial=item.status in {"partial", "incomplete"},
        )
    ]


def enrich_selected_items(
    parts: list[TurnResponseItemDTO], items: Sequence[CanonicalItemRecord], *, include: frozenset[str],
) -> list[TurnResponseItemDTO]:
    """按同一 Item 身份补正文；原消息坐标不再用作内容内的第二次配对。"""
    selected = {item.item_id: item for item in items}
    enriched = []
    for part in parts:
        item = selected.get(part.source.item_id)
        if item is None or part.kind == "final_text":
            enriched.append(part)
            continue
        bodies = response_items_from_internal_item(item, include=include,
            logical_item_ordinal=part.source.logical_item_ordinal,
            elapsed_ms=part.source.elapsed_ms or 0)
        body, = bodies
        if body.kind != part.kind:
            raise RuntimeError(f"选中 Item 的正文类型不一致: {item.item_id}/{part.kind}/{body.kind}")
        enriched.append(part.model_copy(update={"text": body.text, "projection": body.projection, "content": body.content,
            "arguments": body.arguments, "result": body.result, "truncated": body.truncated}))
    return sorted(enriched, key=response_item_order)
