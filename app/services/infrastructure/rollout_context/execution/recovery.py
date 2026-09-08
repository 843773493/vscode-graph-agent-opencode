"""Rollout v2 execution resume/lost recovery owner。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.domain.itemized.enums import (
    CommitKind,
    CommitMode,
    ControlOutcome,
    TurnStatus,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.validation import validate_turn_transition
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_idempotency_commit,
    load_committed_storage_commit,
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RolloutExecutionRecoveryMixin:
    """只拥有 execution resume 与 execution-lost 的恢复状态机。"""

    def resume_turn(
        self,
        thread_id: str,
        *,
        turn_id: str,
        checkpoint_ns: str = "",
    ) -> dict[str, object]:
        """显式恢复 interrupted/执行丢失 Turn；cancelled 永久不可恢复。"""
        strict_text(thread_id, field="session_id")
        strict_text(turn_id, field="turn_id")
        strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                row = connection.execute(
                    "SELECT status FROM turn_records WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Turn 不存在: {turn_id}")
                current_status = strict_text(row[0], field="turn_records.status")
                if current_status not in {"interrupted", "unknown"}:
                    raise ValueError("turn_not_resumable")
                execution_lost = False
                if current_status == "unknown":
                    lost_commit = connection.execute(
                        "SELECT commit_id, outcome, status FROM storage_commits "
                        "WHERE commit_kind = 'terminal_convergence' AND subject_id = ? "
                        "ORDER BY commit_id DESC LIMIT 1",
                        (turn_id,),
                    ).fetchone()
                    if lost_commit is not None:
                        lost_commit_id = strict_non_negative_int(
                            lost_commit[0], field="storage_commits.commit_id"
                        )
                        lost_outcome = strict_text(
                            lost_commit[1], field="storage_commits.outcome"
                        )
                        lost_status = strict_text(
                            lost_commit[2], field="storage_commits.status"
                        )
                        execution_lost = (
                            lost_outcome == ControlOutcome.EXECUTION_LOST.value
                            and lost_status == "committed"
                        )
                        if execution_lost:
                            load_committed_storage_commit(
                                connection,
                                commit_id=lost_commit_id,
                            )
                    if not execution_lost:
                        raise ValueError("turn_not_resumable")
                try:
                    validate_turn_transition(
                        current_status,
                        TurnStatus.ACTIVE.value,
                        explicit_resume=True,
                        execution_lost=execution_lost,
                    )
                except ItemSchemaError as error:
                    raise ValueError("turn_not_resumable") from error
                execution_id = "execution-" + uuid4().hex
                attempt = strict_non_negative_int(
                    connection.execute(
                        "SELECT COALESCE(MAX(attempt), 0) + 1 FROM executions WHERE turn_id = ?",
                        (turn_id,),
                    ).fetchone()[0],
                    field="executions.attempt",
                )
                previous = connection.execute(
                    "SELECT execution_id FROM executions WHERE turn_id = ? ORDER BY attempt DESC LIMIT 1",
                    (turn_id,),
                ).fetchone()
                connection.execute("BEGIN IMMEDIATE")
                _commit_id, _commit_offset = self._append_v2_records_transaction(
                    connection,
                    thread_id,
                    checkpoint_ns,
                    (),
                    commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                    commit_mode=CommitMode.METADATA_ONLY.value,
                    outcome=ControlOutcome.UNKNOWN.value,
                    subject_id=turn_id,
                    idempotency_key=f"resume:{turn_id}:{execution_id}",
                    metadata={
                        "phase": "resume_started",
                        "execution_id": execution_id,
                        "previous_execution_id": previous[0] if previous else None,
                    },
                    begin_transaction=False,
                )
                connection.execute(
                    "INSERT INTO executions(execution_id, turn_id, attempt, execution_ordinal, outcome, resumed_from_execution_id, replay_of_execution_id, created_at) VALUES (?, ?, ?, ?, 'unknown', ?, NULL, ?)",
                    (
                        execution_id,
                        turn_id,
                        attempt,
                        attempt,
                    strict_optional_text(
                        previous[0], field="executions.execution_id"
                    )
                    if previous
                    else None,
                        _now(),
                    ),
                )
                connection.execute(
                    "INSERT INTO turn_execution_links(turn_id, execution_id, execution_role, execution_ordinal, link_idempotency_key, created_at) VALUES (?, ?, 'resume', ?, ?, ?)",
                    (
                        turn_id,
                        execution_id,
                        attempt,
                        f"{turn_id}:resume:{execution_id}",
                        _now(),
                    ),
                )
                connection.execute(
                    "UPDATE turn_records SET status = 'active', last_execution_id = ?, updated_at = ? WHERE turn_id = ?",
                    (execution_id, _now(), turn_id),
                )
                self._commit_connection(connection)
                return {
                    "turn_id": turn_id,
                    "execution_id": execution_id,
                    "attempt": attempt,
                }

    def mark_execution_lost(
        self,
        thread_id: str,
        *,
        turn_id: str,
        execution_id: str | None = None,
        checkpoint_ns: str = "",
        reason: str = "provider_result_commit_missing",
    ) -> dict[str, object]:
        """把 dispatch 后、terminal commit 前丢失的 execution 收敛为 unknown。"""
        strict_text(thread_id, field="session_id")
        strict_text(turn_id, field="turn_id")
        strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        strict_text(reason, field="execution lost reason")
        execution_id = strict_optional_text(execution_id, field="execution_id")
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                turn = connection.execute(
                    "SELECT status, last_execution_id FROM turn_records WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if turn is None:
                    raise KeyError(f"Turn 不存在: {turn_id}")
                current_turn_status = strict_text(
                    turn[0], field="turn_records.status"
                )
                target_execution = execution_id or strict_optional_text(
                    turn[1], field="turn_records.last_execution_id"
                )
                if target_execution is None:
                    raise KeyError(f"Turn 没有可标记 execution: {turn_id}")
                execution = connection.execute(
                    "SELECT execution_id, outcome FROM executions WHERE execution_id = ? AND turn_id = ?",
                    (target_execution, turn_id),
                ).fetchone()
                if execution is None:
                    raise KeyError(
                        f"execution 不存在或不属于 Turn: {target_execution}"
                    )
                current_status = current_turn_status
                execution_outcome = strict_text(
                    execution[1], field="executions.outcome"
                )
                if current_status in {
                    TurnStatus.COMPLETED.value,
                    TurnStatus.COMPLETED_EMPTY.value,
                    TurnStatus.FAILED.value,
                    TurnStatus.CANCELLED.value,
                }:
                    return {
                        "turn_id": turn_id,
                        "execution_id": target_execution,
                        "status": current_status,
                        "outcome": execution_outcome,
                    }
                if (
                    current_status == TurnStatus.UNKNOWN.value
                    and execution_outcome == ControlOutcome.EXECUTION_LOST.value
                ):
                    existing_commit = load_committed_idempotency_commit(
                        connection,
                        commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                        subject_id=turn_id,
                        idempotency_key=f"execution-lost:{turn_id}:{target_execution}",
                        commit_mode=CommitMode.METADATA_ONLY.value,
                        outcome=ControlOutcome.EXECUTION_LOST.value,
                        metadata={
                            "execution_id": target_execution,
                            "reason": reason,
                        },
                        item_count=0,
                    )
                    if existing_commit is None:
                        raise RuntimeError(
                            "Turn 已是 execution_lost/unknown，但 terminal commit 缺失"
                        )
                    return {
                        "turn_id": turn_id,
                        "execution_id": target_execution,
                        "status": TurnStatus.UNKNOWN.value,
                        "outcome": ControlOutcome.EXECUTION_LOST.value,
                        "commit_id": existing_commit.commit_id,
                        "idempotent": True,
                    }
                if current_status != TurnStatus.UNKNOWN.value:
                    try:
                        validate_turn_transition(
                            current_status,
                            TurnStatus.UNKNOWN.value,
                        )
                    except ItemSchemaError as error:
                        raise ValueError(
                            f"非法 execution_lost Turn.status 转移: {current_status}->unknown"
                        ) from error
                connection.execute("BEGIN IMMEDIATE")
                commit_id, _offset = self._append_v2_records_transaction(
                    connection,
                    thread_id,
                    checkpoint_ns,
                    (),
                    commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                    commit_mode=CommitMode.METADATA_ONLY.value,
                    outcome=ControlOutcome.EXECUTION_LOST.value,
                    subject_id=turn_id,
                    idempotency_key=f"execution-lost:{turn_id}:{target_execution}",
                    metadata={
                        "execution_id": target_execution,
                        "reason": reason,
                    },
                    begin_transaction=False,
                )
                timestamp = _now()
                connection.execute(
                    "UPDATE executions SET outcome = 'execution_lost' WHERE execution_id = ? AND turn_id = ?",
                    (target_execution, turn_id),
                )
                connection.execute(
                    "UPDATE model_calls SET outcome = 'unknown', dispatch_state = 'unknown' WHERE execution_id = ?",
                    (target_execution,),
                )
                connection.execute(
                    "UPDATE context_assemblies SET status = 'terminal', outcome = 'execution_lost', terminal_at = ? WHERE turn_id = ? AND execution_id = ? AND status = 'sealed'",
                    (timestamp, turn_id, target_execution),
                )
                connection.execute(
                    "UPDATE turn_records SET status = 'unknown', updated_at = ? WHERE turn_id = ?",
                    (timestamp, turn_id),
                )
                connection.execute(
                    "UPDATE turns SET status = 'failed', updated_at = ? WHERE turn_id = ?",
                    (timestamp, turn_id),
                )
                self._commit_connection(connection)
                return {
                    "turn_id": turn_id,
                    "execution_id": target_execution,
                    "status": TurnStatus.UNKNOWN.value,
                    "outcome": ControlOutcome.EXECUTION_LOST.value,
                    "commit_id": commit_id,
                }


__all__ = ["RolloutExecutionRecoveryMixin"]
