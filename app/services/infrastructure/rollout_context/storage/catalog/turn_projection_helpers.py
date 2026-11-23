"""Turn projection 的纯 identity/兼容统计 helper。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import provider_tool_call_id
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_text,
)


def _finalize_activity_projection(projection: dict[str, object]) -> None:
    """按逻辑 activity 列表计算相邻时间差，首项从 Turn projection.created_at 起算。"""
    activity = projection.get("activity_items")
    if not isinstance(activity, list):
        raise TypeError("Turn activity projection 缺少 activity_items")
    previous_at = projection.get("created_at")
    _required_datetime(previous_at, field="turn_projection.created_at")
    thinking_blocks: list[dict[str, object]] = []
    tool_items: list[dict[str, object]] = []
    for item in activity:
        if not isinstance(item, dict):
            raise TypeError("activity item projection 必须是对象")
        created_at = item.get("created_at")
        item["elapsed_ms"] = _item_elapsed_ms(created_at, item.get("previous_created_at", previous_at))
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
                    "model_call_id": item.get("model_call_id"),
                    "tool_name": item.get("tool_name"),
                    "status": item.get("status"),
                }
            )
        elif item.get("kind") == "text":
            # 非 final 正文属于过程成员，不混入 thinking/tool 专用列表。
            pass
        else:
            raise RuntimeError(f"未知 activity item kind: {item.get('kind')!r}")
    projection["thinking_blocks"] = thinking_blocks
    projection["tool_items"] = tool_items
    activity_stats = projection.get("activity_stats")
    if not isinstance(activity_stats, dict):
        raise TypeError("Turn activity stats projection 字段不完整")
    activity_stats["item_count"] = len(activity)
    # 只从显式活动成员取得本地物理上下界，不用物理坐标推导逻辑顺序或成员。
    item_sequences = [item["item_sequence"] for item in activity]
    activity_stats["first_item_sequence"] = min(item_sequences, default=None)
    activity_stats["last_item_sequence"] = max(item_sequences, default=None)


def _item_elapsed_ms(created_at: object, previous_at: object) -> int:
    """按相邻逻辑成员计算整数毫秒；缺少时区或时间倒退都明确失败。"""
    current = _required_datetime(created_at, field="activity_item.created_at")
    previous = _required_datetime(previous_at, field="activity_item.previous_created_at")
    if current < previous:
        raise RuntimeError(
            f"activity item 创建时间倒退: current={current.isoformat()}, previous={previous.isoformat()}"
        )
    return int((current - previous).total_seconds() * 1000)


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


def _authoritative_tool_coordinates(
    tool_by_call_id: dict[tuple[str, str | None, str], dict[str, object]],
    turn_id: str,
    identity: ItemProjectionIdentity | None,
    call_id: str | None,
) -> dict[str, object]:
    """使用显式 model-call scope 回查派生工具坐标。"""
    if call_id is None:
        return {}
    scope = identity.model_call_id if identity is not None else None
    authoritative = tool_by_call_id.get((turn_id, scope, provider_tool_call_id(identity, call_id)))
    if authoritative is None:
        return {}
    return {
        name: authoritative.get(name) for name in (
            "result_message_sequence", "assistant_message_sequence", "call_index"
        )
    }
