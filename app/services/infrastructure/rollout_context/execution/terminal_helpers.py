"""terminal convergence 使用的 canonical item 与 active view helper。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_text,
)


class RolloutTerminalProjectionMixin:
    """只负责 terminal projection 所需的 durable item/view 辅助读取。"""

    def _read_canonical_item_from_catalog(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        checkpoint_ns: str,
        item_id: str,
    ) -> CanonicalItemRecord:
        """在同一写事务中恢复一个已提交 catalog item，供 projection 补齐。"""
        row = connection.execute(
            "SELECT item_sequence, content_hash, jsonl_offset, jsonl_length "
            "FROM item_catalog WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"canonical item 不存在: {item_id}")
        sequence = strict_non_negative_int(
            row[0], field="item_catalog.item_sequence"
        )
        expected_hash = strict_text(
            row[1], field="item_catalog.content_hash"
        )
        offset = strict_non_negative_int(row[2], field="item_catalog.jsonl_offset")
        length = strict_non_negative_int(row[3], field="item_catalog.jsonl_length")
        if length == 0:
            raise RuntimeError(f"canonical item JSONL locator 长度非法: {item_id}")
        jsonl_path = self.jsonl_path(thread_id, checkpoint_ns)
        with jsonl_path.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(length)
        if len(raw) != length:
            raise RuntimeError(f"canonical item JSONL locator 长度不足: {item_id}")
        envelope = json.loads(raw.decode("utf-8"))
        if not isinstance(envelope, Mapping):
            raise TypeError(f"canonical item JSONL envelope 非法: {item_id}")
        if raw != canonical_json_line(envelope):
            raise RuntimeError(f"canonical item JSONL 不是 canonical line: {item_id}")
        item = CanonicalItemRecord.from_dict(envelope)
        if (
            item.item_id != item_id
            or item.item_sequence != sequence
            or item.content_hash != expected_hash
        ):
            raise RuntimeError(f"canonical item catalog 与 JSONL identity 冲突: {item_id}")
        return item

    @staticmethod
    def _ensure_active_view_turn(
        connection: sqlite3.Connection,
        *,
        checkpoint_ns: str,
        turn_id: str,
    ) -> None:
        """让没有先经过 checkpoint view 的 terminal Turn 也拥有 active 索引。"""
        branch = connection.execute(
            "SELECT active_branch_id FROM checkpoint_namespace_state "
            "WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if branch is None:
            raise RuntimeError(
                f"terminal convergence 缺少 active branch: {checkpoint_ns!r}"
            )
        branch_id = strict_text(
            branch[0], field="checkpoint_namespace_state.active_branch_id"
        )
        view = connection.execute(
            "SELECT head_view_id FROM branches "
            "WHERE branch_id = ? AND status = 'active'",
            (branch_id,),
        ).fetchone()
        turn = connection.execute(
            "SELECT user_message_sequence, final_message_sequence FROM turns "
            "WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        root = connection.execute(
            "SELECT root_input_item_id FROM turn_records WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if view is None or turn is None or root is None:
            raise RuntimeError(f"terminal convergence 无法建立 active Turn view: {turn_id}")
        view_id = strict_text(view[0], field="branches.head_view_id")
        user_sequence = strict_optional_non_negative_int(
            turn[0], field="turns.user_message_sequence"
        )
        final_sequence = strict_optional_non_negative_int(
            turn[1], field="turns.final_message_sequence"
        )
        root_item_id = strict_text(
            root[0], field="turn_records.root_input_item_id"
        )
        existing = connection.execute(
            "SELECT 1 FROM context_view_turns WHERE view_id = ? AND turn_id = ?",
            (view_id, turn_id),
        ).fetchone()
        if existing is not None:
            connection.execute(
                "UPDATE context_view_turns SET user_message_sequence = ?, "
                "final_message_sequence = ?, root_input_item_id = ? "
                "WHERE view_id = ? AND turn_id = ?",
                (user_sequence, final_sequence, root_item_id, view_id, turn_id),
            )
            return
        ordinal = strict_non_negative_int(
            connection.execute(
                "SELECT COALESCE(MAX(logical_turn_ordinal), 0) + 1 "
                "FROM context_view_turns WHERE view_id = ?",
                (view_id,),
            ).fetchone()[0],
            field="context_view_turns.logical_turn_ordinal",
        )
        connection.execute(
            "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, "
            "user_message_sequence, final_message_sequence, root_input_item_id, "
            "fork_lineage_json) VALUES (?, ?, ?, ?, ?, ?, '{}')",
            (
                view_id,
                turn_id,
                ordinal,
                user_sequence,
                final_sequence,
                root_item_id,
            ),
        )


__all__ = ["RolloutTerminalProjectionMixin"]
