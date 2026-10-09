"""v2 Turn 的 message projection 与 accepted-root reconciliation。"""

from __future__ import annotations

import sqlite3

from app.domain.itemized.enums import SemanticKind, TurnScope
from app.domain.itemized.execution_admission import user_turn_admission_payload_hash
from app.domain.itemized.hashing import validate_hash_token
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_optional_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutTurnIndexMixin:
    """只负责从已提交 message/item projection 维护 Turn 快速索引。"""


    def _ensure_v2_turn_record_from_root(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        item: CanonicalItemRecord,
        branch_id: str,
        timestamp: str,
    ) -> None:
        """校验 checkpoint root 已由真实 Turn admission 建立。"""
        session_id = strict_text(session_id, field="turn_records.session_id")
        thread_id = strict_text(thread_id, field="turn_records.thread_id")
        strict_text(branch_id, field="turn_records.source_branch_id")
        strict_text(timestamp, field="turn_records.created_at")
        if (
            item.semantic_kind != SemanticKind.USER_INPUT.value
            or item.turn_scope != TurnScope.TURN_ROOT.value
            or item.turn_id is None
        ):
            return
        turn = connection.execute(
            "SELECT session_id, thread_id, root_input_item_id, "
            "root_input_item_sequence, accepted_ingress_id, "
            "acceptance_idempotency_key, initial_execution_id, last_execution_id "
            "FROM turn_records WHERE turn_id = ?",
            (item.turn_id,),
        ).fetchone()
        if turn is None:
            raise RuntimeError(
                "checkpoint user root 缺少 acceptance-time Turn admission: "
                f"session_id={session_id} thread_id={thread_id} turn_id={item.turn_id}"
            )
        turn_owner = (
            strict_text(turn[0], field="turn_records.session_id"),
            strict_text(turn[1], field="turn_records.thread_id"),
        )
        root_item_id = strict_text(
            turn[2], field=f"turn_records.root_input_item_id: {item.turn_id}"
        )
        root_item_sequence = strict_optional_non_negative_int(
            turn[3], field=f"turn_records.root_input_item_sequence: {item.turn_id}"
        )
        accepted_ingress_id = strict_text(
            turn[4], field=f"turn_records.accepted_ingress_id: {item.turn_id}"
        )
        acceptance_key = strict_text(
            turn[5], field=f"turn_records.acceptance_idempotency_key: {item.turn_id}"
        )
        execution_id = strict_text(
            turn[6], field=f"turn_records.initial_execution_id: {item.turn_id}"
        )
        last_execution_id = strict_text(
            turn[7], field=f"turn_records.last_execution_id: {item.turn_id}"
        )
        if turn_owner != (session_id, thread_id):
            raise RuntimeError(
                "checkpoint root Turn owner 不匹配: "
                f"turn_id={item.turn_id} stored={turn_owner!r} "
                f"expected={(session_id, thread_id)!r}"
            )
        if root_item_id != item.item_id or root_item_sequence != item.item_sequence:
            raise RuntimeError(f"v2 Turn root identity 冲突: turn_id={item.turn_id}")

        acceptance = connection.execute(
            "SELECT session_id, thread_id, turn_id, accepted_ingress_id, "
            "acceptance_idempotency_key, payload_hash FROM turn_acceptances "
            "WHERE accepted_ingress_id = ? AND acceptance_idempotency_key = ?",
            (accepted_ingress_id, acceptance_key),
        ).fetchone()
        if acceptance is None:
            raise RuntimeError(
                f"checkpoint root 缺少已接受的 owner acceptance: turn_id={item.turn_id}"
            )
        acceptance_owner = (
            strict_text(acceptance[0], field="turn_acceptances.session_id"),
            strict_text(acceptance[1], field="turn_acceptances.thread_id"),
        )
        acceptance_turn_id = strict_text(
            acceptance[2], field="turn_acceptances.turn_id"
        )
        acceptance_ingress_id = strict_text(
            acceptance[3], field="turn_acceptances.accepted_ingress_id"
        )
        stored_acceptance_key = strict_text(
            acceptance[4], field="turn_acceptances.acceptance_idempotency_key"
        )
        payload_hash = validate_hash_token(
            acceptance[5], "turn_acceptances.payload_hash"
        )
        if (
            acceptance_owner != (session_id, thread_id)
            or acceptance_turn_id != item.turn_id
            or acceptance_ingress_id != accepted_ingress_id
            or stored_acceptance_key != acceptance_key
            or payload_hash != item.content_hash
        ):
            raise RuntimeError(
                f"checkpoint root acceptance identity 冲突: turn_id={item.turn_id}"
            )

        execution = connection.execute(
            "SELECT session_id, thread_id, turn_id, execution_origin, job_id, "
            "admission_idempotency_key, admission_payload_hash, accepted_ingress_id "
            "FROM executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if execution is None:
            raise RuntimeError(
                f"checkpoint root 缺少初始 execution admission: turn_id={item.turn_id}"
            )
        execution_owner = (
            strict_text(execution[0], field="executions.session_id"),
            strict_text(execution[1], field="executions.thread_id"),
        )
        execution_turn_id = strict_text(execution[2], field="executions.turn_id")
        execution_origin = strict_text(
            execution[3], field="executions.execution_origin"
        )
        job_id = strict_text(execution[4], field="executions.job_id")
        admission_key = strict_text(
            execution[5], field="executions.admission_idempotency_key"
        )
        admission_hash = validate_hash_token(
            execution[6], "executions.admission_payload_hash"
        )
        execution_ingress_id = strict_optional_text(
            execution[7], field="executions.accepted_ingress_id"
        )
        expected_admission_hash = user_turn_admission_payload_hash(
            session_id=session_id,
            thread_id=thread_id,
            turn_id=item.turn_id,
            execution_id=execution_id,
            job_id=job_id,
            accepted_ingress_id=accepted_ingress_id,
            admission_idempotency_key=admission_key,
            payload_hash=payload_hash,
        )
        if (
            execution_owner != (session_id, thread_id)
            or execution_turn_id != item.turn_id
            or execution_origin != "user_turn"
            or execution_ingress_id != accepted_ingress_id
            or admission_hash != expected_admission_hash
        ):
            raise RuntimeError(
                f"checkpoint root execution admission 与 acceptance 不匹配: turn_id={item.turn_id}"
            )
        link = connection.execute(
            "SELECT 1 FROM turn_execution_links WHERE session_id = ? "
            "AND thread_id = ? AND turn_id = ? AND execution_id = ? "
            "AND execution_role = 'initial' AND execution_ordinal = 1",
            (session_id, thread_id, item.turn_id, execution_id),
        ).fetchone()
        if link is None:
            raise RuntimeError(
                f"checkpoint root 缺少 initial execution link: turn_id={item.turn_id}"
            )
        last_execution = connection.execute(
            "SELECT 1 FROM executions WHERE session_id = ? AND thread_id = ? "
            "AND turn_id = ? AND execution_id = ?",
            (session_id, thread_id, item.turn_id, last_execution_id),
        ).fetchone()
        if last_execution is None:
            raise RuntimeError(
                f"checkpoint root last execution owner 不匹配: turn_id={item.turn_id}"
            )


__all__ = ["RolloutTurnIndexMixin"]
