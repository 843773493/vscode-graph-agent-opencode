"""full-copy 源 canonical item catalog 的严格读取与唯一坐标校验。"""

from __future__ import annotations

import sqlite3

from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
    optional_text,
    required_text,
)


def read_full_copy_item_rows(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    item_rows = tuple(
        connection.execute(
            "SELECT item_sequence, item_id, turn_id, jsonl_offset, jsonl_length, projection_manifest_json FROM item_catalog ORDER BY item_sequence"
        ).fetchall()
    )
    normalized_item_rows: list[tuple[object, ...]] = []
    seen_item_sequences: set[int] = set()
    seen_item_ids: set[str] = set()
    for row in item_rows:
        if len(row) != 6:
            raise RuntimeError("full_rollout_copy item catalog 字段数量不一致")
        sequence = non_negative_int(row[0], field="item_catalog.item_sequence")
        if sequence == 0 or sequence in seen_item_sequences:
            raise RuntimeError(
                f"full_rollout_copy item sequence 非法或重复: {sequence}"
            )
        item_id = required_text(row[1], field="item_catalog.item_id")
        if item_id in seen_item_ids:
            raise RuntimeError(f"full_rollout_copy item identity 重复: {item_id}")
        turn_id = optional_text(row[2], field="item_catalog.turn_id")
        offset = non_negative_int(row[3], field="item_catalog.jsonl_offset")
        length = non_negative_int(row[4], field="item_catalog.jsonl_length")
        if length == 0:
            raise RuntimeError(f"full_rollout_copy item JSONL length 为 0: {item_id}")
        if not isinstance(row[5], str) or not row[5]:
            raise RuntimeError(
                f"full_rollout_copy item projection_manifest_json 非空字符串: {item_id}"
            )
        normalized_item_rows.append(
            (sequence, item_id, turn_id, offset, length, row[5])
        )
        seen_item_sequences.add(sequence)
        seen_item_ids.add(item_id)
    item_rows = tuple(normalized_item_rows)
    return item_rows


def read_full_copy_overlay_epochs(
    connection: sqlite3.Connection, session_id: str, thread_id: str,
) -> list[int]:
    """严格读取 source overlay epoch，保持复制前相对顺序。"""
    source_overlay_epochs = sorted(
        {
            non_negative_int(row[0], field="source_overlays.source_overlay_epoch")
            for row in connection.execute(
                "SELECT source_overlay_epoch FROM source_overlays "
                "WHERE session_id = ? AND thread_id = ?",
                (session_id, thread_id),
            ).fetchall()
        }
    )
    return source_overlay_epochs
