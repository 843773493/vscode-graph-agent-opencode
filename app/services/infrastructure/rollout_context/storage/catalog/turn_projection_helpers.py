"""Turn projection 的纯 identity/兼容统计 helper。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
)


def _finalize_activity_projection(projection: dict[str, object]) -> None:
    """从后端已解析的逻辑 activity item 生成统计与兼容投影。"""
    activity = projection.get("activity_items")
    if not isinstance(activity, list):
        raise TypeError("Turn activity projection 缺少 activity_items")
    previous_at = _required_datetime(
        projection.get("created_at"), field="turn_projection.created_at"
    )
    thinking_blocks: list[dict[str, object]] = []
    tool_items: list[dict[str, object]] = []
    for item in activity:
        if not isinstance(item, dict):
            raise TypeError("activity item projection 必须是对象")
        created_at = _required_datetime(
            item.get("created_at"), field="activity_item.created_at"
        )
        item["elapsed_ms"] = max(
            0,
            int((created_at - previous_at).total_seconds() * 1000),
        )
        previous_at = created_at
        if item.get("kind") in {
            "reasoning",
            "reasoning_summary",
            "reasoning_encrypted",
            "compaction_summary",
        }:
            compatibility_kind = (
                "summary"
                if item.get("kind") in {"reasoning_summary", "compaction_summary"}
                else "encrypted"
                if item.get("kind") == "reasoning_encrypted"
                else "reasoning"
            )
            thinking_blocks.append(
                {
                    "kind": compatibility_kind,
                    "text": item.get("text", ""),
                }
            )
        elif item.get("kind") in {"tool_call", "tool_result"}:
            tool_items.append(
                {
                    "item_kind": item["kind"],
                    "sequence": item.get("message_sequence", 0),
                    "assistant_message_sequence": item.get(
                        "assistant_message_sequence"
                    ),
                    "result_message_sequence": item.get(
                        "result_message_sequence"
                    ),
                    "call_index": item.get("call_index"),
                    "tool_call_id": item.get("tool_call_id"),
                    "tool_name": item.get("tool_name"),
                    "status": item.get("status"),
                }
            )
        elif item.get("kind") == "text":
            # assistant_output 正文只作为非 final text part 进入时间线，
            # 不进入 thinking/tool 统计。completed Turn 的中间正文仍由 final
            # pointer 承载避免双重投影；非 completed 终态 Turn 没有 final
            # pointer，其 completed 正文也在此投影，否则历史会丢失正文。
            pass
        else:
            raise RuntimeError(f"未知 activity item kind: {item.get('kind')!r}")
    projection["thinking_blocks"] = thinking_blocks
    projection["tool_items"] = tool_items
    activity_stats = projection.get("activity_stats")
    if not isinstance(activity_stats, dict):
        raise TypeError("Turn activity stats projection 字段不完整")
    activity_stats["item_count"] = len(activity)
    activity_stats["first_item_sequence"] = (
        activity[0]["item_sequence"] if activity else None
    )
    activity_stats["last_item_sequence"] = (
        activity[-1]["item_sequence"] if activity else None
    )


def _required_datetime(value: object, *, field: str) -> datetime:
    text = strict_text(value, field=field)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise RuntimeError(f"{field} 缺少时区")
    return parsed.astimezone(UTC)


def _json_object(value: object, *, field: str) -> dict[str, object]:
    text = strict_text(value, field=field)
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{field} 不是合法 JSON") from error
    if not isinstance(decoded, dict):
        raise TypeError(f"{field} 必须是对象")
    return decoded


def _normalized_model_call_id(producer_ref: dict[str, object]) -> str:
    producer_id = strict_text(
        producer_ref.get("producer_id"), field="producer_ref.producer_id"
    )
    return producer_id.removeprefix("lc_run--")


def _activity_model_call_id(
    metadata: dict[str, object],
    producer_ref: dict[str, object],
) -> str:
    # TODO: 旧 canonical item 补齐 model_call_id 后，删除 producer_ref 回退。
    model_call_id = metadata.get("model_call_id")
    if isinstance(model_call_id, str) and model_call_id:
        return model_call_id
    return _normalized_model_call_id(producer_ref)


def _authoritative_tool_coordinates(
    tool_by_call_id: dict[tuple[str, str], dict[str, object]],
    turn_id: str,
    block_id: object,
) -> dict[str, object]:
    """用 scoped block_id 内嵌的原始 call ID 回查 SQLite 权威工具坐标。

    实时 canonical tool_call 只携带 model-call scoped block_id，且派生
    ``tool_calls`` 表可能尚未提交，因此拿不到 ``assistant_message_sequence`` /
    ``call_index`` / ``result_message_sequence``。这些坐标是后续定点详情按显式
    位置回填参数/结果的唯一稳定依据；缺失时只能退化为“原始 call ID 全局唯一”
    的脆弱匹配，一旦 ID 复用就会静默丢参数。这里从 scoped ID 反解 provider
    原始 ID，再回查 SQLite 已提交的权威坐标。

    TODO: 实时 canonical tool_call 直接携带坐标后，本回退即可删除。
    """
    if not isinstance(block_id, str) or not block_id:
        return {}
    raw_call_id = _raw_call_id_from_scoped(block_id)
    if raw_call_id is None:
        return {}
    authoritative = tool_by_call_id.get((turn_id, raw_call_id))
    if authoritative is None:
        return {}
    return {
        "result_message_sequence": authoritative.get("result_message_sequence"),
        "assistant_message_sequence": authoritative.get("assistant_message_sequence"),
        "call_index": authoritative.get("call_index"),
    }


def _logical_activity_key(item: dict[str, object]) -> tuple[object, ...]:
    """只按持久 identity/provenance 合并同一逻辑 item，绝不比较正文。"""
    kind = strict_text(item.get("kind"), field="activity_item.kind")
    if kind == "tool_call":
        producer_ref = item.get("producer_ref")
        if not isinstance(producer_ref, dict):
            raise TypeError("tool_call activity item 缺少 producer_ref")
        return (
            kind,
            strict_text(item.get("tool_call_id"), field="tool_call_id"),
            _normalized_model_call_id(producer_ref),
            strict_non_negative_int(item.get("call_index"), field="call_index"),
        )
    if kind == "tool_result":
        return (
            kind,
            strict_text(item.get("tool_call_id"), field="tool_call_id"),
        )
    if kind == "text":
        return (kind, strict_text(item.get("item_id"), field="item_id"))
    if kind == "compaction_summary":
        return (kind, strict_text(item.get("item_id"), field="item_id"))
    producer_ref = item.get("producer_ref")
    if not isinstance(producer_ref, dict):
        raise TypeError("reasoning activity item 缺少 producer_ref")
    block_ordinal = item.get("block_ordinal")
    if not isinstance(block_ordinal, int) or isinstance(block_ordinal, bool):
        raise TypeError("reasoning activity item 缺少 block_ordinal")
    block_id = item.get("block_id")
    if isinstance(block_id, str) and block_id:
        return (
            kind,
            _normalized_model_call_id(producer_ref),
            "block_id",
            block_id,
        )
    return (kind, _normalized_model_call_id(producer_ref), block_ordinal)


def _final_reasoning_source_refs(
    final_item_metadata: dict[str, object],
    *,
    content_block_index: int,
    item_index: int,
    provider_item_id: object,
) -> set[str]:
    """解析最终 checkpoint reasoning 指向的持久 content part identity。"""
    refs: set[str] = set()
    if provider_item_id is not None:
        refs.add(strict_text(provider_item_id, field="reasoning_blocks.item_id"))

    # reasoning_items 中第二个及后续匿名子项不能只凭外层 block ref 合并；
    # 它们没有足够精确的持久 identity，应继续作为独立逻辑 Item。
    if item_index != 0:
        return refs
    raw_part_refs = final_item_metadata.get("content_part_refs")
    if raw_part_refs is None:
        return refs
    if not isinstance(raw_part_refs, list):
        raise TypeError("final item metadata.content_part_refs 必须是列表")
    matching_ids: list[str] = []
    for ordinal, raw_ref in enumerate(raw_part_refs):
        if not isinstance(raw_ref, dict):
            raise TypeError(
                f"final item metadata.content_part_refs[{ordinal}] 必须是对象"
            )
        ref_index = raw_ref.get("index")
        if not isinstance(ref_index, int) or isinstance(ref_index, bool):
            raise TypeError(
                f"final item metadata.content_part_refs[{ordinal}].index 必须是整数"
            )
        if ref_index != content_block_index:
            continue
        matching_ids.append(
            strict_text(
                raw_ref.get("id"),
                field=f"final item metadata.content_part_refs[{ordinal}].id",
            )
        )
    if len(matching_ids) > 1:
        raise RuntimeError(
            "final item metadata.content_part_refs 存在重复 content block index"
        )
    refs.update(matching_ids)
    return refs


def _source_ref_matches(
    source_ref: str,
    seen_refs: set[str],
) -> bool:
    """同时匹配 provider 原始 part ID 与 scoped block ID。"""
    # TODO: 历史 scoped block ID 完成迁移后，删除 scoped 后缀兼容匹配。
    if source_ref in seen_refs:
        return True
    return any(
        seen_ref.endswith(f":block:{source_ref}")
        for seen_ref in seen_refs
    )


def _raw_call_id_from_scoped(block_id: str) -> str | None:
    """从 model-call scoped block_id 取出 checkpoint 中的 provider 原始 call ID。"""
    marker = ":tool-call:"
    if marker not in block_id:
        return None
    return block_id.rsplit(marker, 1)[-1] or None
