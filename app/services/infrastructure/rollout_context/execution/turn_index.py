"""v2 Turn 的 message projection 与 checkpoint-origin root reconciliation。"""

from __future__ import annotations

import sqlite3

from app.domain.itemized.enums import SemanticKind, TurnScope
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutTurnIndexMixin:
    """只负责从已提交 message/item projection 维护 Turn 快速索引。"""

    def _upsert_turn(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
        sequence: int,
        message_id: str,
        role: str,
        branch_id: str,
        timestamp: str,
    ) -> None:
        turn_id = strict_text(turn_id, field="turns.turn_id")
        sequence = strict_non_negative_int(sequence, field="turns.message_sequence")
        if sequence == 0:
            raise ValueError(f"turns.message_sequence 必须为正数: {turn_id}")
        message_id = strict_text(message_id, field="turns.message_id")
        role = strict_text(role, field="turns.role")
        branch_id = strict_text(branch_id, field="turns.branch_id")
        timestamp = strict_text(timestamp, field="turns.updated_at")
        row = connection.execute(
            "SELECT first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence, final_message_id FROM turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            if role != "user" or turn_id.startswith("internal-"):
                return
            ordinal = strict_non_negative_int(
                connection.execute(
                    "SELECT COALESCE(MAX(turn_ordinal), 0) + 1 FROM turns"
                ).fetchone()[0],
                field="turns.turn_ordinal",
            )
            if ordinal == 0:
                raise RuntimeError("turns.turn_ordinal 必须为正数")
            connection.execute(
                "INSERT INTO turns(turn_id, turn_ordinal, turn_kind, branch_id, first_message_sequence, last_message_sequence, user_message_sequence, final_message_sequence, final_message_id, status, created_at, updated_at) VALUES (?, ?, 'normal', ?, ?, ?, ?, NULL, NULL, 'running', ?, ?)",
                (
                    turn_id,
                    ordinal,
                    branch_id,
                    sequence,
                    sequence,
                    sequence,
                    timestamp,
                    timestamp,
                ),
            )
            return
        first_sequence = strict_non_negative_int(
            row[0], field=f"turns.first_message_sequence: {turn_id}"
        )
        last_sequence = strict_non_negative_int(
            row[1], field=f"turns.last_message_sequence: {turn_id}"
        )
        user_sequence_stored = strict_optional_non_negative_int(
            row[2], field=f"turns.user_message_sequence: {turn_id}"
        )
        final_sequence = strict_optional_non_negative_int(
            row[3], field=f"turns.final_message_sequence: {turn_id}"
        )
        final_message_id = strict_optional_text(
            row[4], field=f"turns.final_message_id: {turn_id}"
        )
        if final_sequence is not None and final_message_id is None:
            raise RuntimeError(f"turns final message identity 缺失: {turn_id}")
        if first_sequence == 0 or last_sequence == 0:
            raise RuntimeError(f"turns message sequence 必须为正数: {turn_id}")
        if last_sequence < first_sequence:
            raise RuntimeError(f"turns message sequence 倒序: {turn_id}")
        user_sequence = (
            user_sequence_stored
            if user_sequence_stored is not None
            else sequence
            if role == "user"
            else None
        )
        reopened = (
            final_sequence is not None
            and role in {"assistant", "tool"}
            and sequence > final_sequence
        )
        updated = connection.execute(
            "UPDATE turns SET last_message_sequence = ?, user_message_sequence = COALESCE(user_message_sequence, ?), final_message_sequence = ?, final_message_id = ?, status = ?, updated_at = ? WHERE turn_id = ?",
            (
                sequence,
                user_sequence,
                None if reopened else final_sequence,
                None if reopened else final_message_id,
                "running"
                if reopened
                else "completed"
                if final_sequence is not None
                else "running",
                timestamp,
                turn_id,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"turns 更新失败: {turn_id}")

    def _ensure_v2_turn_record_from_root(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        item: CanonicalItemRecord,
        branch_id: str,
        timestamp: str,
    ) -> None:
        """为显式 checkpoint root 补齐 v2 acceptance/execution lineage。"""
        thread_id = strict_text(thread_id, field="turn_records.session_id")
        branch_id = strict_text(branch_id, field="turn_records.source_branch_id")
        timestamp = strict_text(timestamp, field="turn_records.created_at")
        if (
            item.semantic_kind != SemanticKind.USER_INPUT.value
            or item.turn_scope != TurnScope.TURN_ROOT.value
            or item.turn_id is None
        ):
            return
        existing = connection.execute(
            "SELECT turn_id, root_input_item_id FROM turn_records WHERE turn_id = ?",
            (item.turn_id,),
        ).fetchone()
        if existing is not None:
            existing_root_id = strict_text(
                existing[1], field=f"turn_records.root_input_item_id: {item.turn_id}"
            )
            if existing_root_id != item.item_id:
                raise RuntimeError(f"v2 Turn root identity 冲突: turn_id={item.turn_id}")
            return
        identity_seed = sha256_jcs(
            {
                "session_id": thread_id,
                "turn_id": item.turn_id,
                "root_item_id": item.item_id,
                "root_content_hash": item.content_hash,
            }
        )
        accepted_ingress_id = f"checkpoint-ingress:{identity_seed}"
        acceptance_key = f"checkpoint-acceptance:{identity_seed}"
        execution_id = f"checkpoint-execution:{identity_seed}"
        conflict = connection.execute(
            "SELECT accepted_ingress_id, acceptance_idempotency_key, payload_hash "
            "FROM turn_acceptances WHERE accepted_ingress_id = ? "
            "OR acceptance_idempotency_key = ?",
            (accepted_ingress_id, acceptance_key),
        ).fetchall()
        if conflict:
            raise RuntimeError(
                "checkpoint root acceptance identity 已被不同 lineage 占用"
            )
        ordinal = strict_non_negative_int(
            connection.execute(
                "SELECT COALESCE(MAX(turn_ordinal), 0) + 1 FROM turn_records"
            ).fetchone()[0],
            field="turn_records.turn_ordinal",
        )
        if ordinal == 0:
            raise RuntimeError("turn_records.turn_ordinal 必须为正数")
        connection.execute(
            "INSERT INTO turn_acceptances(accepted_ingress_id, acceptance_idempotency_key, session_id, turn_id, payload_hash, identity_origin, created_at) VALUES (?, ?, ?, ?, ?, 'checkpoint_origin', ?)",
            (
                accepted_ingress_id,
                acceptance_key,
                thread_id,
                item.turn_id,
                item.content_hash,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO turn_records(turn_id, turn_ordinal, source_branch_id, root_input_item_id, root_input_item_sequence, accepted_ingress_id, acceptance_idempotency_key, initial_execution_id, last_execution_id, status, final_item_id, replay_of_turn_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, ?, ?)",
            (
                item.turn_id,
                ordinal,
                branch_id,
                item.item_id,
                item.item_sequence,
                accepted_ingress_id,
                acceptance_key,
                execution_id,
                execution_id,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO executions(execution_id, turn_id, attempt, execution_ordinal, accepted_ingress_id, outcome, created_at) VALUES (?, ?, 1, 1, ?, 'unknown', ?)",
            (execution_id, item.turn_id, accepted_ingress_id, timestamp),
        )
        connection.execute(
            "INSERT INTO turn_execution_links(turn_id, execution_id, execution_role, execution_ordinal, link_idempotency_key, created_at) VALUES (?, ?, 'initial', 1, ?, ?)",
            (
                item.turn_id,
                execution_id,
                f"{item.turn_id}:initial:{execution_id}",
                timestamp,
            ),
        )


__all__ = ["RolloutTurnIndexMixin"]
