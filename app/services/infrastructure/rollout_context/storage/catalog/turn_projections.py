"""v2 catalog 的 Turn projection/page SQL 查询。"""

from __future__ import annotations

from collections.abc import Iterable
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
        message_count_rows = connection.execute(
            f"SELECT turn_id, COUNT(*) FROM messages WHERE turn_id IN ({placeholders}) GROUP BY turn_id",
            ids,
        ).fetchall()
        tool_rows = connection.execute(
            f"SELECT m.turn_id, tc.tool_call_id, tc.tool_name, tc.status, tc.result_message_sequence, tc.assistant_message_sequence, tc.call_index FROM tool_calls tc JOIN messages m ON m.message_sequence = tc.assistant_message_sequence WHERE m.turn_id IN ({placeholders}) ORDER BY tc.assistant_message_sequence, tc.call_index",
            ids,
        ).fetchall()
        final_rows = connection.execute(
            "SELECT t.turn_id, t.final_message_sequence, mp.visible_text, "
            "mp.visible_text_truncated, m.message_id, m.turn_id, "
            "item.item_id, item.turn_id, item.semantic_kind, item.status "
            "FROM turns t "
            "LEFT JOIN message_projections mp ON mp.message_sequence = t.final_message_sequence "
            "LEFT JOIN messages m ON m.message_sequence = t.final_message_sequence "
            "LEFT JOIN item_catalog item ON item.jsonl_offset = m.jsonl_offset "
            "AND item.jsonl_length = m.jsonl_length "
            f"WHERE t.turn_id IN ({placeholders})",
            ids,
        ).fetchall()
        thinking_rows = connection.execute(
            f"SELECT m.turn_id, rb.message_sequence, rb.content_block_index, rb.item_index, rb.carrier_type, rb.reasoning_text, rb.summary_text, rb.signature_present, rb.encrypted_length FROM reasoning_blocks rb JOIN messages m ON m.message_sequence = rb.message_sequence WHERE m.turn_id IN ({placeholders}) ORDER BY rb.message_sequence, rb.content_block_index, rb.item_index",
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
                "created_at": created_at,
                "updated_at": updated_at,
                "activity_stats": {
                    "duration_ms": None,
                    "message_count": 0,
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
        for turn_id, count in message_count_rows:
            turn_id = strict_text(turn_id, field="messages.turn_id")
            count = strict_non_negative_int(count, field=f"messages.count:{turn_id}")
            projection = result.get(turn_id)
            if projection is None:
                raise RuntimeError(f"messages 缺少 turns projection: {turn_id}")
            activity_stats = projection["activity_stats"]
            if isinstance(activity_stats, dict):
                activity_stats["message_count"] = count
        for (
            turn_id, final_sequence, text, truncated, message_id, message_turn_id,
            item_id, item_turn_id, semantic_kind, item_status,
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
                raise RuntimeError(
                    f"final message projection 缺少 turns row: {turn_id}"
                )
            result[turn_id]["final_response_text"] = text or ""
            result[turn_id]["final_response_text_truncated"] = truncated_value == 1
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
        ) in thinking_rows:
            turn_id = strict_text(turn_id, field="messages.turn_id")
            message_sequence = strict_non_negative_int(
                message_sequence, field=f"reasoning_blocks.message_sequence:{turn_id}"
            )
            content_block_index = strict_non_negative_int(
                content_block_index,
                field=f"reasoning_blocks.content_block_index:{turn_id}",
            )
            item_index = strict_non_negative_int(
                item_index, field=f"reasoning_blocks.item_index:{turn_id}"
            )
            carrier_type = strict_text(
                carrier_type, field=f"reasoning_blocks.carrier_type:{turn_id}"
            )
            reasoning_text = strict_optional_text(
                reasoning_text, field=f"reasoning_blocks.reasoning_text:{turn_id}"
            )
            summary_text = strict_optional_text(
                summary_text, field=f"reasoning_blocks.summary_text:{turn_id}"
            )
            signature_present_value = strict_non_negative_int(
                signature_present,
                field=f"reasoning_blocks.signature_present:{turn_id}",
            )
            if signature_present_value not in {0, 1}:
                raise RuntimeError(f"reasoning signature 标记非法: {turn_id}")
            encrypted_length = strict_optional_non_negative_int(
                encrypted_length,
                field=f"reasoning_blocks.encrypted_length:{turn_id}",
            )
            if turn_id not in result:
                raise RuntimeError(f"reasoning projection 缺少 turns row: {turn_id}")
            blocks = result[turn_id]["thinking_blocks"]
            if isinstance(blocks, list):
                source = {
                    "message_sequence": message_sequence,
                    "carrier_type": carrier_type,
                    "content_block_index": content_block_index,
                    "item_index": item_index,
                    "signature_present": signature_present_value == 1,
                }
                if reasoning_text:
                    blocks.append(
                        {"kind": "reasoning", "text": reasoning_text, **source}
                    )
                elif summary_text:
                    blocks.append({"kind": "summary", "text": summary_text, **source})
                elif encrypted_length is not None:
                    blocks.append({"kind": "encrypted", "text": "", **source})
            if encrypted_length is not None:
                result[turn_id]["has_encrypted_reasoning"] = True
        for (
            turn_id,
            call_id,
            name,
            status,
            result_sequence,
            assistant_sequence,
            call_index,
        ) in tool_rows:
            turn_id = strict_text(turn_id, field="messages.turn_id")
            call_id = strict_text(call_id, field=f"tool_calls.tool_call_id:{turn_id}")
            name = strict_text(name, field=f"tool_calls.tool_name:{call_id}")
            status = strict_text(status, field=f"tool_calls.status:{call_id}")
            result_sequence = strict_optional_non_negative_int(
                result_sequence, field=f"tool_calls.result_message_sequence:{call_id}"
            )
            assistant_sequence = strict_non_negative_int(
                assistant_sequence,
                field=f"tool_calls.assistant_message_sequence:{call_id}",
            )
            call_index = strict_non_negative_int(
                call_index, field=f"tool_calls.call_index:{call_id}"
            )
            if assistant_sequence == 0:
                raise RuntimeError(
                    f"tool call assistant message sequence 不能为 0: {call_id}"
                )
            if turn_id not in result:
                raise RuntimeError(f"tool projection 缺少 turns row: {turn_id}")
            items = result[turn_id]["tool_items"]
            if isinstance(items, list):
                items.append(
                    {
                        "sequence": assistant_sequence,
                        "call_index": call_index,
                        "item_kind": "tool_call",
                        "tool_name": name,
                        "tool_call_id": call_id,
                        "status": status,
                    }
                )
                if result_sequence is not None:
                    items.append(
                        {
                            "sequence": result_sequence,
                            "assistant_message_sequence": assistant_sequence,
                            "call_index": call_index,
                            "item_kind": "tool_result",
                            "tool_name": name,
                            "tool_call_id": call_id,
                            "status": status,
                        }
                    )
        return result

    def decode_indexed_message(
        self, value: object, *, summary_only: bool = False
    ) -> object:
        del summary_only
        if not isinstance(value, dict):
            raise TypeError("rollout message 必须是对象")
        return self._codec().from_dict(value)
