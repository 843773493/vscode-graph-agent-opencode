"""Turn projection 的有序成员间隔与轻量展示映射。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_text,
)


def _finalize_activity_projection(projection: dict[str, object]) -> None:
    """消费原索引前驱计算成员间隔；整轮统计由同一索引在分页前确定。"""
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
        item["elapsed_ms"] = 0 if item.get("kind") == "reasoning_encrypted" else _item_elapsed_ms(
            created_at, item.get("previous_created_at", previous_at)
        )
        if item.get("kind") != "reasoning_encrypted":
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
        elif item.get("kind") in {"text", "content"}:
            # 非 final 正文属于过程成员，不混入 thinking/tool 专用列表。
            pass
        else:
            raise RuntimeError(f"未知 activity item kind: {item.get('kind')!r}")
    projection["thinking_blocks"] = thinking_blocks
    projection["tool_items"] = tool_items


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
