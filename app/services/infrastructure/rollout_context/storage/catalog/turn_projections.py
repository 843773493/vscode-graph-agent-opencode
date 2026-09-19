"""v2 catalog 的 Turn projection/page SQL 查询。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.storage.primitives import (
        RolloutReadSnapshot,
    )


VISIBLE_NORMAL_TURN_PREDICATE = (
    "EXISTS (SELECT 1 FROM turn_records AS canonical_turn "
    "JOIN item_catalog AS root ON root.item_id = canonical_turn.root_input_item_id "
    "JOIN context_view_items AS membership ON membership.item_id = root.item_id "
    "AND membership.view_id = cvt.view_id "
    "WHERE canonical_turn.turn_id = cvt.turn_id "
    "AND cvt.root_input_item_id = canonical_turn.root_input_item_id "
    "AND root.turn_id = canonical_turn.turn_id "
    "AND root.semantic_kind = 'user_input' AND root.turn_scope = 'turn_root' "
    "AND root.status = 'completed' AND membership.visible = 1)"
)
_V2_TO_HISTORY_STATUS = {
    "open": "accepted",
    "active": "running",
    "completed": "completed",
    "completed_empty": "completed",
    "interrupted": "timed_out",
    "cancelled": "cancelled",
    "failed": "failed",
    "unknown": "failed",
}


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


def _raw_call_id_from_scoped(block_id: str) -> str | None:
    """从 model-call scoped block_id 取出 checkpoint 中的 provider 原始 call ID。"""
    marker = ":tool-call:"
    if marker not in block_id:
        return None
    return block_id.rsplit(marker, 1)[-1] or None


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


def _turn_page_row(row: tuple[object, ...]) -> tuple[str, int, int, int]:
    if len(row) != 4:
        raise RuntimeError("Turn projection row 字段数量不一致")
    turn_id = strict_text(row[0], field="context_view_turns.turn_id")
    first_sequence = strict_non_negative_int(
        row[1], field=f"turns.first_message_sequence:{turn_id}"
    )
    last_sequence = strict_non_negative_int(
        row[2], field=f"turns.last_message_sequence:{turn_id}"
    )
    ordinal = strict_non_negative_int(
        row[3], field=f"context_view_turns.logical_turn_ordinal:{turn_id}"
    )
    if first_sequence == 0 or last_sequence == 0 or last_sequence < first_sequence:
        raise RuntimeError(f"Turn projection message range 非法: {turn_id}")
    return turn_id, first_sequence, last_sequence, ordinal


class TurnProjectionQueryMixin:
    """读取已提交 Turn projection/cursor，不承担业务 DTO 规则。"""

    def read_context_turn_page(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        *,
        direction: str,
        anchor_ordinal: int | None,
        limit: int,
    ) -> tuple[list[tuple[str, int, int, int]], bool]:
        """按逻辑 Turn 序号执行 keyset 分页，并多读一行判断是否还有数据。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        if direction not in {"tail", "before", "head", "after"}:
            raise ValueError(f"不支持的 Turn keyset 方向: {direction}")
        limit = strict_non_negative_int(limit, field="turn_page.limit")
        if limit < 1:
            raise ValueError("Turn keyset limit 必须大于 0")
        anchor_ordinal = strict_optional_non_negative_int(
            anchor_ordinal, field="turn_page.anchor_ordinal"
        )
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        query_limit = limit + 1
        params: list[object] = [view_id]
        where = "cvt.view_id = ?"
        order = "ASC"
        if direction == "tail":
            order = "DESC"
        elif direction == "before":
            if anchor_ordinal is None:
                raise ValueError("before keyset 必须提供 anchor_ordinal")
            where += " AND cvt.logical_turn_ordinal < ?"
            params.append(anchor_ordinal)
            order = "DESC"
        elif direction == "head":
            order = "ASC"
        elif direction == "after":
            if anchor_ordinal is None:
                raise ValueError("after keyset 必须提供 anchor_ordinal")
            where += " AND cvt.logical_turn_ordinal > ?"
            params.append(anchor_ordinal)
            order = "ASC"
        rows = connection.execute(
            f"SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, cvt.logical_turn_ordinal "
            f"FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
            f"WHERE {where} AND {VISIBLE_NORMAL_TURN_PREDICATE} ORDER BY cvt.logical_turn_ordinal {order} LIMIT ?",
            (*params, query_limit),
        ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        if direction in {"tail", "before"}:
            selected.reverse()
        return [_turn_page_row(row) for row in selected], has_more

    def read_context_turn_window(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        *,
        anchor_ordinal: int,
        before: int,
        after: int,
    ) -> list[tuple[str, int, int, int]]:
        """只读取游标附近的逻辑 Turn，用于 around 请求。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        anchor_ordinal = strict_non_negative_int(
            anchor_ordinal, field="turn_window.anchor_ordinal"
        )
        before = strict_non_negative_int(before, field="turn_window.before")
        after = strict_non_negative_int(after, field="turn_window.after")
        if anchor_ordinal < 1:
            raise ValueError("around keyset 参数非法")
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        rows = connection.execute(
            "SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, cvt.logical_turn_ordinal "
            "FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
            f"WHERE cvt.view_id = ? AND cvt.logical_turn_ordinal BETWEEN ? AND ? AND {VISIBLE_NORMAL_TURN_PREDICATE} "
            "ORDER BY cvt.logical_turn_ordinal",
            (view_id, max(1, anchor_ordinal - before), anchor_ordinal + after),
        ).fetchall()
        return [_turn_page_row(row) for row in rows]

    def read_context_turn_ids(
        self,
        snapshot: RolloutReadSnapshot,
        view_id: str,
        turn_ids: Iterable[str],
    ) -> list[tuple[str, int, int, int]]:
        """按指定 Turn ID 读取当前 view 中存在的 Turn，并按逻辑序号排序。"""
        view_id = strict_text(view_id, field="context_views.view_id")
        ids = tuple(
            dict.fromkeys(
                strict_text(turn_id, field="turn_ids.turn_id") for turn_id in turn_ids
            )
        )
        if not ids:
            return []
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT cvt.turn_id, t.first_message_sequence, t.last_message_sequence, cvt.logical_turn_ordinal "
            f"FROM context_view_turns cvt JOIN turns t ON t.turn_id = cvt.turn_id "
            f"WHERE cvt.view_id = ? AND cvt.turn_id IN ({placeholders}) AND {VISIBLE_NORMAL_TURN_PREDICATE} "
            "ORDER BY cvt.logical_turn_ordinal",
            (view_id, *ids),
        ).fetchall()
        return [_turn_page_row(row) for row in rows]

    def read_turn_projections(
        self, snapshot: RolloutReadSnapshot, turn_ids: Iterable[str]
    ) -> dict[str, dict[str, object]]:
        ids = tuple(
            dict.fromkeys(
                strict_text(turn_id, field="turn_ids.turn_id") for turn_id in turn_ids
            )
        )
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        connection = self._snapshot_connection(snapshot)
        self._require_v2_runtime(connection)
        turns = connection.execute(
            f"SELECT turn_id, first_message_sequence, last_message_sequence, final_message_sequence, final_message_id, status, created_at, updated_at FROM turns WHERE turn_id IN ({placeholders})",
            ids,
        ).fetchall()
        v2_turns = connection.execute(
            f"SELECT turn_id, status, final_item_id FROM turn_records WHERE turn_id IN ({placeholders})",
            ids,
        ).fetchall()
        tool_rows = connection.execute(
            f"SELECT m.turn_id, m.message_id, tc.tool_call_id, tc.tool_name, tc.status, tc.result_message_sequence, tc.assistant_message_sequence, tc.call_index FROM tool_calls tc JOIN messages m ON m.message_sequence = tc.assistant_message_sequence WHERE m.turn_id IN ({placeholders}) ORDER BY tc.assistant_message_sequence, tc.call_index",
            ids,
        ).fetchall()
        final_rows = connection.execute(
            "SELECT t.turn_id, t.final_message_sequence, mp.visible_text, "
            "mp.visible_text_truncated, m.message_id, m.turn_id, "
            "item.item_id, item.turn_id, item.semantic_kind, item.status, "
            "item.item_sequence, item.created_at "
            "FROM turns t "
            "LEFT JOIN message_projections mp ON mp.message_sequence = t.final_message_sequence "
            "LEFT JOIN messages m ON m.message_sequence = t.final_message_sequence "
            "LEFT JOIN item_catalog item ON item.jsonl_offset = m.jsonl_offset "
            "AND item.jsonl_length = m.jsonl_length "
            f"WHERE t.turn_id IN ({placeholders})",
            ids,
        ).fetchall()
        activity_rows = connection.execute(
            "SELECT ic.turn_id, ic.item_sequence, ic.item_id, ic.semantic_kind, "
            "ic.payload_kind, ic.status, ic.created_at, ic.producer_ref_json, "
            "ic.metadata_json, ip.content, ip.content_truncated "
            "FROM item_catalog AS ic JOIN item_projections AS ip "
            "ON ip.item_id = ic.item_id AND ip.item_sequence = ic.item_sequence "
            f"WHERE ic.turn_id IN ({placeholders}) AND ic.semantic_kind IN "
            "('reasoning','tool_call','tool_result','compaction_summary') "
            "ORDER BY ic.turn_id, ic.item_sequence",
            ids,
        ).fetchall()
        final_reasoning_rows = connection.execute(
            "SELECT t.turn_id, rb.message_sequence, rb.content_block_index, "
            "rb.item_index, rb.carrier_type, rb.reasoning_text, rb.summary_text, "
            "rb.signature_present, rb.encrypted_length, rb.item_id, ic.item_id, "
            "ic.item_sequence, ic.created_at, ic.metadata_json "
            "FROM turns AS t "
            "JOIN turn_records AS tr ON tr.turn_id = t.turn_id "
            "JOIN item_catalog AS ic ON ic.item_id = tr.final_item_id "
            "JOIN reasoning_blocks AS rb ON rb.message_sequence = t.final_message_sequence "
            f"WHERE t.turn_id IN ({placeholders}) "
            "ORDER BY t.turn_id, rb.content_block_index, rb.item_index",
            ids,
        ).fetchall()
        result: dict[str, dict[str, object]] = {}
        for row in turns:
            if len(row) != 8:
                raise RuntimeError("Turn projection source row 字段数量不一致")
            turn_id = strict_text(row[0], field="turns.turn_id")
            first_sequence = strict_non_negative_int(
                row[1], field=f"turns.first_message_sequence:{turn_id}"
            )
            last_sequence = strict_non_negative_int(
                row[2], field=f"turns.last_message_sequence:{turn_id}"
            )
            final_sequence = strict_optional_non_negative_int(
                row[3], field=f"turns.final_message_sequence:{turn_id}"
            )
            if (
                first_sequence == 0
                or last_sequence == 0
                or last_sequence < first_sequence
            ):
                raise RuntimeError(f"Turn projection message range 非法: {turn_id}")
            final_message_id = strict_optional_text(
                row[4], field=f"turns.final_message_id:{turn_id}"
            )
            status = strict_text(row[5], field=f"turns.status:{turn_id}")
            created_at = strict_text(row[6], field=f"turns.created_at:{turn_id}")
            updated_at = strict_text(row[7], field=f"turns.updated_at:{turn_id}")
            result[turn_id] = {
                "first_sequence": first_sequence,
                "last_sequence": last_sequence,
                "final_message_sequence": final_sequence,
                "final_message_id": final_message_id,
                "status": status,
                "final_source": "turn_finalize" if final_sequence is not None else None,
                "final_response_text": "",
                "final_response_text_truncated": False,
                "thinking_blocks": [],
                "assistant_text_sequences": [],
                "has_encrypted_reasoning": False,
                "tool_items": [],
                "activity_items": [],
                "created_at": created_at,
                "updated_at": updated_at,
                "activity_stats": {
                    "duration_ms": None,
                    "item_count": 0,
                    "first_item_sequence": None,
                    "last_item_sequence": None,
                },
            }
        # TurnRecord 是 v2 的生命周期事实源；旧 turns 行仅保留 message
        # sequence projection，不能覆盖失败、取消或执行丢失等 v2 状态。
        for turn_id, status, final_item_id in v2_turns:
            turn_id = strict_text(turn_id, field="turn_records.turn_id")
            status = strict_text(status, field=f"turn_records.status:{turn_id}")
            if status not in _V2_TO_HISTORY_STATUS:
                raise RuntimeError(f"未知 v2 Turn status: {status}")
            final_item_id = strict_optional_text(
                final_item_id, field=f"turn_records.final_item_id:{turn_id}"
            )
            projection = result.get(turn_id)
            if projection is None:
                raise RuntimeError(f"TurnRecord 缺少 turns projection: {turn_id}")
            projection["status"] = _V2_TO_HISTORY_STATUS[status]
            if final_item_id is not None:
                projection["final_item_id"] = final_item_id
        for (
            turn_id, final_sequence, text, truncated, message_id, message_turn_id,
            item_id, item_turn_id, semantic_kind, item_status,
            final_item_sequence, final_item_created_at,
        ) in final_rows:
            turn_id = strict_text(turn_id, field="turns.turn_id")
            final_sequence = strict_optional_non_negative_int(
                final_sequence, field=f"turns.final_message_sequence:{turn_id}"
            )
            if final_sequence is None and text is None and truncated is None:
                if result[turn_id].get("final_item_id") is not None:
                    raise RuntimeError(f"canonical final item 缺少 message projection: {turn_id}")
                continue
            projection = result[turn_id]
            if (
                item_id is None
                or item_id != projection.get("final_item_id")
                or message_id != projection["final_message_id"]
                or item_turn_id != turn_id
                or message_turn_id != turn_id
                or semantic_kind != "assistant_output"
                or item_status != "completed"
                or projection["status"] != "completed"
            ):
                raise RuntimeError(f"final projection 与 canonical Turn/item 指针不一致: {turn_id}")
            if text is None or truncated is None:
                raise RuntimeError(f"final message projection 字段不完整: {turn_id}")
            text = strict_optional_text(
                text, field=f"message_projections.visible_text:{turn_id}"
            )
            truncated_value = strict_non_negative_int(
                truncated, field=f"message_projections.visible_text_truncated:{turn_id}"
            )
            if truncated_value not in {0, 1}:
                raise RuntimeError(f"message projection truncated 标记非法: {turn_id}")
            if turn_id not in result:
                raise TypeError(
                    f"final message projection 缺少 turns row: {turn_id}"
                )
            result[turn_id]["final_response_text"] = text or ""
            result[turn_id]["final_response_text_truncated"] = truncated_value == 1
            result[turn_id]["final_item_sequence"] = strict_non_negative_int(
                final_item_sequence,
                field=f"item_catalog.item_sequence:{turn_id}",
            )
            result[turn_id]["final_item_created_at"] = strict_text(
                final_item_created_at,
                field=f"item_catalog.created_at:{turn_id}",
            )
        tool_by_message: dict[tuple[str, str], list[dict[str, object]]] = {}
        tool_by_call_id: dict[tuple[str, str], dict[str, object]] = {}
        tool_by_model_call_index: dict[tuple[str, str, int], dict[str, object]] = {}
        canonical_tools_by_model_call: dict[
            tuple[str, str], list[dict[str, object]]
        ] = {}
        for (
            turn_id,
            message_id,
            call_id,
            name,
            status,
            result_sequence,
            assistant_sequence,
            call_index,
        ) in tool_rows:
            turn_id = strict_text(turn_id, field="messages.turn_id")
            message_id = strict_text(message_id, field="messages.message_id")
            call_id = strict_text(call_id, field=f"tool_calls.tool_call_id:{turn_id}")
            tool = {
                "tool_call_id": call_id,
                "tool_name": strict_text(name, field=f"tool_calls.tool_name:{call_id}"),
                "status": strict_text(status, field=f"tool_calls.status:{call_id}"),
                "result_message_sequence": strict_optional_non_negative_int(
                    result_sequence,
                    field=f"tool_calls.result_message_sequence:{call_id}",
                ),
                "assistant_message_sequence": strict_non_negative_int(
                    assistant_sequence,
                    field=f"tool_calls.assistant_message_sequence:{call_id}",
                ),
                "call_index": strict_non_negative_int(
                    call_index, field=f"tool_calls.call_index:{call_id}"
                ),
            }
            tool_by_message.setdefault((turn_id, message_id), []).append(tool)
            tool_by_call_id[(turn_id, call_id)] = tool
            tool_by_model_call_index[
                (turn_id, message_id.removeprefix("lc_run--"), tool["call_index"])
            ] = tool

        canonical_tool_call_ids_by_model_call: dict[
            tuple[str, str, int], str
        ] = {}
        canonical_tool_call_ids_by_raw_id: dict[tuple[str, str], str | None] = {}
        # 实时消息流的 tool-call 使用 model-call scoped ID；checkpoint shadow
        # 仍携带 provider 原始 ID。先建立 canonical 的 (模型调用, call index)
        # 映射，后续所有兼容消息都通过这张表归一化。
        for (
            turn_id,
            _item_sequence,
            _item_id,
            semantic_kind,
            _payload_kind,
            _item_status,
            _item_created_at,
            producer_ref_json,
            metadata_json,
            _content,
            _content_truncated,
        ) in activity_rows:
            if semantic_kind != "tool_call":
                continue
            metadata = _json_object(
                metadata_json, field="item_catalog.metadata_json:canonical-tool"
            )
            block_id = metadata.get("block_id")
            if not isinstance(block_id, str) or not block_id:
                continue
            producer_ref = _json_object(
                producer_ref_json, field="item_catalog.producer_ref_json:canonical-tool"
            )
            model_call_id = _activity_model_call_id(metadata, producer_ref)
            block_index = metadata.get("block_index")
            if not isinstance(block_index, int) or isinstance(block_index, bool):
                raise TypeError(
                    "canonical tool_call 缺少稳定 block_index: "
                    f"turn_id={turn_id} block_id={block_id}"
                )
            canonical_tool_call_ids_by_model_call[
                (turn_id, model_call_id, block_index)
            ] = block_id

        seen_activity: dict[str, set[tuple[object, ...]]] = {
            turn_id: set() for turn_id in result
        }
        seen_reasoning_source_refs: dict[str, set[str]] = {
            turn_id: set() for turn_id in result
        }
        canonical_reasoning_keys: dict[
            str, dict[tuple[str, str, int], tuple[object, ...]]
        ] = {turn_id: {} for turn_id in result}
        shadow_reasoning_indices: dict[str, dict[tuple[str, str, int], int]] = {
            turn_id: {} for turn_id in result
        }
        discarded_activity_indices: dict[str, set[int]] = {
            turn_id: set() for turn_id in result
        }
        for (
            turn_id,
            item_sequence,
            item_id,
            semantic_kind,
            payload_kind,
            item_status,
            item_created_at,
            producer_ref_json,
            metadata_json,
            content,
            content_truncated,
        ) in activity_rows:
            turn_id = strict_text(turn_id, field="item_catalog.turn_id")
            metadata = _json_object(
                metadata_json, field=f"item_catalog.metadata_json:{item_id}"
            )
            producer_ref = _json_object(
                producer_ref_json, field=f"item_catalog.producer_ref_json:{item_id}"
            )
            semantic_kind = strict_text(
                semantic_kind, field=f"item_catalog.semantic_kind:{item_id}"
            )
            source_part_id = metadata.get("block_id")
            projection_message_id = metadata.get("projection_message_id")
            activity_model_call_id = _activity_model_call_id(metadata, producer_ref)
            matching_tools = (
                tool_by_message.get((turn_id, projection_message_id), [])
                if isinstance(projection_message_id, str)
                else []
            )
            block_ordinal = metadata.get("block_index")
            projection_group = metadata.get("projection_group")
            if not isinstance(block_ordinal, int) or isinstance(block_ordinal, bool):
                block_ordinal = (
                    projection_group.get("ordinal")
                    if isinstance(projection_group, dict)
                    else 0
                )
            block_ordinal = strict_non_negative_int(
                block_ordinal, field=f"activity_item.block_ordinal:{item_id}"
            )
            activity_tools: list[dict[str, object] | None] = [None]
            if semantic_kind in {"tool_call", "tool_result"}:
                metadata_call_id = metadata.get("tool_call_id")
                block_id = metadata.get("block_id")
                tool_call_id: str | None = None
                if isinstance(metadata_call_id, str) and metadata_call_id:
                    tool_call_id = metadata_call_id
                elif isinstance(block_id, str) and block_id:
                    tool_call_id = block_id
                if tool_call_id is not None:
                    selected_tool = tool_by_call_id.get((turn_id, tool_call_id))
                    if selected_tool is None:
                        selected_tool = tool_by_model_call_index.get(
                            (turn_id, activity_model_call_id, block_ordinal)
                        )
                    canonical_tool_call_id: str | None = None
                    raw_id_alias = canonical_tool_call_ids_by_raw_id.get(
                        (turn_id, tool_call_id)
                    )
                    if raw_id_alias is not None:
                        canonical_tool_call_id = raw_id_alias
                    if selected_tool is not None:
                        canonical_tool_call_id = canonical_tool_call_id or (
                            canonical_tool_call_ids_by_model_call.get(
                                (
                                    turn_id,
                                    activity_model_call_id,
                                    strict_non_negative_int(
                                        selected_tool.get("call_index"),
                                        field="tool_calls.call_index",
                                    ),
                                )
                            )
                        )
                    elif isinstance(block_id, str) and block_id:
                        canonical_tool_call_id = block_id
                    if canonical_tool_call_id is not None:
                        if selected_tool is None:
                            if semantic_kind != "tool_call":
                                raise RuntimeError(
                                    "canonical tool_result 缺少对应 tool_call: "
                                    f"{item_id}"
                                )
                            coordinates = _authoritative_tool_coordinates(
                                tool_by_call_id, turn_id, block_id
                            )
                            selected_tool = {
                                "tool_call_id": canonical_tool_call_id,
                                "tool_name": strict_text(
                                    content,
                                    field=f"item_projections.content:{item_id}",
                                ),
                                "status": strict_text(
                                    item_status,
                                    field=f"item_catalog.status:{item_id}",
                                ),
                                "result_message_sequence": coordinates.get(
                                    "result_message_sequence"
                                ),
                                "assistant_message_sequence": coordinates.get(
                                    "assistant_message_sequence"
                                ),
                                "call_index": coordinates.get(
                                    "call_index", block_ordinal
                                ),
                            }
                        else:
                            selected_tool = {
                                **selected_tool,
                                "tool_call_id": canonical_tool_call_id,
                            }
                        selected_tool.setdefault("call_index", block_ordinal)
                        if isinstance(block_id, str) and block_id:
                            tool_by_call_id[(turn_id, canonical_tool_call_id)] = (
                                selected_tool
                            )
                            model_call_key = (
                                turn_id,
                                activity_model_call_id,
                            )
                            canonical_tools_by_model_call.setdefault(
                                model_call_key,
                                [],
                            ).append(selected_tool)
                    if selected_tool is None:
                        if semantic_kind != "tool_call":
                            raise RuntimeError(
                                "canonical tool_result 缺少对应 tool_call: "
                                f"{item_id}"
                            )
                        # 实时 canonical tool_call 先于 LangChain message
                        # projection 提交时，item 自身就是历史摘要的权威来源。
                        # 后续 checkpoint shadow 通过 model-call provenance 和
                        # tool_call_id 在本方法内合并，不能因派生表尚未存在而 500。
                        coordinates = _authoritative_tool_coordinates(
                            tool_by_call_id, turn_id, block_id
                        )
                        selected_tool = {
                            "tool_call_id": tool_call_id,
                            "tool_name": strict_text(
                                content,
                                field=f"item_projections.content:{item_id}",
                            ),
                            "status": strict_text(
                                item_status,
                                field=f"item_catalog.status:{item_id}",
                            ),
                            "result_message_sequence": coordinates.get(
                                "result_message_sequence"
                            ),
                            "assistant_message_sequence": coordinates.get(
                                "assistant_message_sequence"
                            ),
                            "call_index": coordinates.get(
                                "call_index", block_ordinal
                            ),
                        }
                        tool_by_call_id[(turn_id, tool_call_id)] = selected_tool
                        model_call_key = (
                            turn_id,
                            activity_model_call_id,
                        )
                        canonical_tools_by_model_call.setdefault(
                            model_call_key, []
                        ).append(selected_tool)
                    if semantic_kind == "tool_result":
                        selected_tool = {
                            **selected_tool,
                            "status": strict_text(
                                item_status,
                                field=f"item_catalog.status:{item_id}",
                            ),
                            "result_message_sequence": selected_tool.get(
                                "result_message_sequence"
                            ),
                        }
                    activity_tools = [selected_tool]
                elif matching_tools:
                    # 一个 assistant_output carrier 可以包含多个 tool_calls；
                    # 它们共享物理 item offset，但每个 call 都是独立逻辑 Item。
                    # checkpoint message 仍携带 provider 原始 call id，必须先
                    # 用同一 model-call 的 call_index 映射回 canonical identity，
                    # 否则同一个工具会在历史中同时出现 raw call 和 canonical call。
                    normalized_tools: list[dict[str, object]] = []
                    model_call_key = (
                        turn_id,
                        activity_model_call_id,
                    )
                    for matching_tool in matching_tools:
                        canonical_tool_call_id = (
                            canonical_tool_call_ids_by_model_call.get(
                                (
                                    *model_call_key,
                                    strict_non_negative_int(
                                        matching_tool.get("call_index"),
                                        field="tool_calls.call_index",
                                    ),
                                )
                            )
                        )
                        if canonical_tool_call_id is None:
                            normalized_tools.append(matching_tool)
                            continue
                        normalized_tool = {
                            **matching_tool,
                            "tool_call_id": canonical_tool_call_id,
                        }
                        tool_by_call_id[(turn_id, canonical_tool_call_id)] = (
                            normalized_tool
                        )
                        raw_tool_call_id = matching_tool.get("tool_call_id")
                        if isinstance(raw_tool_call_id, str) and raw_tool_call_id:
                            alias_key = (turn_id, raw_tool_call_id)
                            if alias_key not in canonical_tool_call_ids_by_raw_id:
                                canonical_tool_call_ids_by_raw_id[alias_key] = (
                                    canonical_tool_call_id
                                )
                            elif (
                                canonical_tool_call_ids_by_raw_id[alias_key]
                                != canonical_tool_call_id
                            ):
                                # 同一 raw ID 映射到多个 model-call 时不能猜测
                                # result 属于哪一次，保持未归一化以避免误合并。
                                canonical_tool_call_ids_by_raw_id[alias_key] = None
                        normalized_tools.append(normalized_tool)
                    activity_tools = normalized_tools
                else:
                    model_call_tools = canonical_tools_by_model_call.get(
                        (turn_id, activity_model_call_id),
                        [],
                    )
                    if semantic_kind == "tool_call" and model_call_tools:
                        # ensure_request_items 生成的 checkpoint shadow
                        # 可能只在 typed payload 中保存 call id。其 model-call
                        # provenance 与先到的实时 item 一致，直接复用后端已经
                        # 解析出的逻辑工具身份，不读取正文或依赖相邻关系。
                        activity_tools = list(model_call_tools)
                    else:
                        raise RuntimeError(
                            f"canonical {semantic_kind} 缺少稳定 tool_call_id: {item_id}"
                        )
            kind = (
                "reasoning_summary"
                if semantic_kind == "reasoning" and payload_kind == "summary"
                else "reasoning_encrypted"
                if semantic_kind == "reasoning" and payload_kind in {"opaque", "extension"}
                else semantic_kind
            )
            if (
                semantic_kind == "reasoning"
                and isinstance(source_part_id, str)
                and source_part_id
            ):
                seen_reasoning_source_refs[turn_id].add(source_part_id)
            for activity_tool in activity_tools:
                activity_item: dict[str, object] = {
                    "item_id": strict_text(item_id, field="item_catalog.item_id"),
                    "item_sequence": strict_non_negative_int(
                        item_sequence, field=f"item_catalog.item_sequence:{item_id}"
                    ),
                    "part_ordinal": 0,
                    "kind": kind,
                    "status": strict_text(item_status, field="item_catalog.status"),
                    "created_at": strict_text(
                        item_created_at, field=f"item_catalog.created_at:{item_id}"
                    ),
                    "text": strict_text(
                        content,
                        field=f"item_projections.content:{item_id}",
                        allow_empty=True,
                    ),
                    "truncated": strict_non_negative_int(
                        content_truncated,
                        field=f"item_projections.content_truncated:{item_id}",
                    )
                    == 1,
                    "producer_ref": producer_ref,
                    "block_ordinal": block_ordinal,
                    "block_id": (
                        source_part_id
                        if isinstance(source_part_id, str) and source_part_id
                        else None
                    ),
                    "message_sequence": 0,
                }
                if activity_tool is not None:
                    activity_item.update(activity_tool)
                    activity_item["tool_call_id"] = strict_text(
                        activity_tool.get("tool_call_id"),
                        field="tool_calls.tool_call_id",
                    )
                    activity_item["part_ordinal"] = strict_non_negative_int(
                        activity_tool.get("call_index"),
                        field="tool_calls.call_index",
                    )
                logical_key = _logical_activity_key(activity_item)
                reasoning_block_key = (
                    (
                        kind,
                        activity_model_call_id,
                        block_ordinal,
                    )
                    if semantic_kind == "reasoning"
                    else None
                )
                is_checkpoint_reasoning_shadow = (
                    semantic_kind == "reasoning"
                    and activity_item["block_id"] is None
                    and isinstance(projection_group, dict)
                )
                if (
                    is_checkpoint_reasoning_shadow
                    and reasoning_block_key is not None
                    and reasoning_block_key in canonical_reasoning_keys[turn_id]
                ):
                    continue
                if logical_key in seen_activity[turn_id]:
                    continue
                seen_activity[turn_id].add(logical_key)
                items = result[turn_id]["activity_items"]
                if not isinstance(items, list):
                    raise TypeError("Turn activity_items projection 必须是列表")
                if (
                    is_checkpoint_reasoning_shadow
                    and reasoning_block_key is not None
                ):
                    shadow_reasoning_indices[turn_id][reasoning_block_key] = len(items)
                elif reasoning_block_key is not None and activity_item["block_id"]:
                    canonical_reasoning_keys[turn_id].setdefault(
                        reasoning_block_key,
                        logical_key,
                    )
                    shadow_index = shadow_reasoning_indices[turn_id].pop(
                        reasoning_block_key,
                        None,
                    )
                    if shadow_index is not None:
                        discarded_activity_indices[turn_id].add(shadow_index)
                items.append(activity_item)

        for (
            turn_id,
            message_sequence,
            content_block_index,
            item_index,
            carrier_type,
            reasoning_text,
            summary_text,
            signature_present,
            encrypted_length,
            reasoning_item_id,
            item_id,
            item_sequence,
            item_created_at,
            final_item_metadata_json,
        ) in final_reasoning_rows:
            turn_id = strict_text(turn_id, field="turns.turn_id")
            content_block_index = strict_non_negative_int(
                content_block_index,
                field=f"reasoning_blocks.content_block_index:{turn_id}",
            )
            item_index = strict_non_negative_int(
                item_index, field=f"reasoning_blocks.item_index:{turn_id}"
            )
            encrypted_length = strict_optional_non_negative_int(
                encrypted_length,
                field=f"reasoning_blocks.encrypted_length:{turn_id}",
            )
            if reasoning_text:
                kind, text = "reasoning", strict_text(
                    reasoning_text, field=f"reasoning_blocks.reasoning_text:{turn_id}"
                )
            elif summary_text:
                kind, text = "reasoning_summary", strict_text(
                    summary_text, field=f"reasoning_blocks.summary_text:{turn_id}"
                )
            elif encrypted_length is not None:
                kind, text = "reasoning_encrypted", ""
                result[turn_id]["has_encrypted_reasoning"] = True
            else:
                continue
            signature_value = strict_non_negative_int(
                signature_present,
                field=f"reasoning_blocks.signature_present:{turn_id}",
            )
            if signature_value not in {0, 1}:
                raise RuntimeError(f"reasoning signature 标记非法: {turn_id}")
            final_item_metadata = _json_object(
                final_item_metadata_json,
                field=f"item_catalog.metadata_json:{item_id}",
            )
            source_refs = _final_reasoning_source_refs(
                final_item_metadata,
                content_block_index=content_block_index,
                item_index=item_index,
                provider_item_id=reasoning_item_id,
            )
            if any(
                _source_ref_matches(
                    source_ref,
                    seen_reasoning_source_refs[turn_id],
                )
                for source_ref in source_refs
            ):
                continue
            seen_reasoning_source_refs[turn_id].update(source_refs)
            items = result[turn_id]["activity_items"]
            if not isinstance(items, list):
                raise TypeError("Turn activity_items projection 必须是列表")
            items.append(
                {
                    "item_id": strict_text(item_id, field="item_catalog.item_id"),
                    "item_sequence": strict_non_negative_int(
                        item_sequence, field="item_catalog.item_sequence"
                    ),
                    "part_ordinal": content_block_index * 1_000_000 + item_index,
                    "kind": kind,
                    "status": "completed",
                    "created_at": strict_text(
                        item_created_at, field="item_catalog.created_at"
                    ),
                    "text": text,
                    "truncated": False,
                    "message_sequence": strict_non_negative_int(
                        message_sequence, field="reasoning_blocks.message_sequence"
                    ),
                    "content_block_index": content_block_index,
                    "item_index": item_index,
                    "carrier_type": strict_text(
                        carrier_type, field="reasoning_blocks.carrier_type"
                    ),
                    "signature_present": signature_value == 1,
                }
            )
        for turn_id, projection in result.items():
            activity_items = projection["activity_items"]
            if not isinstance(activity_items, list):
                raise TypeError("Turn activity_items projection 必须是列表")
            discarded_indices = discarded_activity_indices[turn_id]
            if discarded_indices:
                projection["activity_items"] = activity_items = [
                    item
                    for index, item in enumerate(activity_items)
                    if index not in discarded_indices
                ]
            activity_items.sort(
                key=lambda item: (
                    strict_non_negative_int(
                        item.get("item_sequence"), field="activity_item.item_sequence"
                    ),
                    strict_non_negative_int(
                        item.get("part_ordinal", 0), field="activity_item.part_ordinal"
                    ),
                )
            )
            _finalize_activity_projection(projection)
        return result

    def decode_indexed_message(
        self, value: object, *, summary_only: bool = False
    ) -> object:
        del summary_only
        if not isinstance(value, dict):
            raise TypeError("rollout message 必须是对象")
        return self._codec().from_dict(value)
