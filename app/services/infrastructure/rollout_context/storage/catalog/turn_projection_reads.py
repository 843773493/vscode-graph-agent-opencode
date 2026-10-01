"""v2 catalog 的 Turn projection 读取与 canonical activity 归并。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.services.infrastructure.rollout_context.storage.catalog.turn_activity_merge import (
    _finalize_projections,
    _merge_activity_items,
    _merge_final_reasoning,
)
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


class TurnProjectionReadMixin:
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
            "LEFT JOIN turn_records AS tr ON tr.turn_id = ic.turn_id "
            f"WHERE ic.turn_id IN ({placeholders}) AND (ic.semantic_kind IN "
            "('reasoning','tool_call','tool_result','compaction_summary') "
            "OR (ic.semantic_kind = 'assistant_output' AND (ic.status = 'partial' "
            "OR (ic.status = 'completed' AND tr.status IN "
            "('failed','cancelled','interrupted','unknown'))))) "
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
        discarded_activity_indices, seen_reasoning_source_refs = (
            _merge_activity_items(result, tool_rows, activity_rows)
        )
        _merge_final_reasoning(
            result, final_reasoning_rows, seen_reasoning_source_refs
        )
        _finalize_projections(result, discarded_activity_indices)
        return result

    def decode_indexed_message(
        self, value: object, *, summary_only: bool = False
    ) -> object:
        del summary_only
        if not isinstance(value, dict):
            raise TypeError("rollout message 必须是对象")
        return self._codec().from_dict(value)
