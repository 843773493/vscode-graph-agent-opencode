"""terminal convergence 使用的 canonical item 与 active view helper。"""

from __future__ import annotations

import sqlite3

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_text,
)


class RolloutTerminalProjectionMixin:
    """只负责 terminal projection 所需的 durable item/view 辅助读取。"""



    def _ensure_active_view_turn(
        self,
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
            "SELECT user_message_sequence, final_message_sequence, root_input_item_id "
            "FROM context_view_turns WHERE view_id = ? AND turn_id = ?",
            (view_id, turn_id),
        ).fetchone()
        if existing == (user_sequence, final_sequence, root_item_id):
            return
        view_id = self._storage._writable_active_context_view_id(connection, checkpoint_ns, branch_id)
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
