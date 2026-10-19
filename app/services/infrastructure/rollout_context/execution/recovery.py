"""Rollout v2 execution resume/lost recovery owner。"""

from __future__ import annotations

from uuid import uuid4

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import (
    CommitKind,
    CommitMode,
    ControlOutcome,
    TurnStatus,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.execution_admission import user_turn_admission_payload_hash
from app.domain.itemized.validation import validate_turn_transition
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_resumable_turn,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_storage_commit,
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutExecutionRecoveryMixin:
    """只拥有 execution resume 与 execution-lost 的恢复状态机。"""

    def resume_turn(self, session_id: str, *, thread_id: str, job_id: str, turn_id: str, checkpoint_ns: str='') -> dict[str, object]:
        """显式恢复 interrupted/执行丢失 Turn；cancelled 永久不可恢复。"""
        strict_text(session_id, field='session_id')
        strict_text(turn_id, field='turn_id')
        strict_text(checkpoint_ns, field='checkpoint_ns', allow_empty=True)
        job_id = strict_text(job_id, field='job_id')
        owner_thread_id = self._storage._resolve_thread_identity(session_id, strict_text(thread_id, field="thread_id"))
        with self._storage._lock(owner_thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(session_id, checkpoint_ns, thread_id=owner_thread_id)
            with self._storage._connect(owner_thread_id, checkpoint_ns, session_id=session_id) as connection:
                self._storage._require_v2_runtime(connection)
                require_resumable_turn(connection, session_id, owner_thread_id, turn_id)
                row = connection.execute('SELECT status, accepted_ingress_id FROM turn_records WHERE session_id = ? AND thread_id = ? AND turn_id = ?', (session_id, owner_thread_id, turn_id)).fetchone()
                if row is None:
                    raise KeyError(f'Turn 不存在: {turn_id}')
                current_status = strict_text(row[0], field='turn_records.status')
                if current_status not in {'interrupted', 'unknown'}:
                    raise ValueError('turn_not_resumable')
                execution_lost = False
                if current_status == 'unknown':
                    lost_commit = connection.execute("SELECT commit_id, outcome, status FROM storage_commits WHERE commit_kind = 'terminal_convergence' AND subject_id = ? ORDER BY commit_id DESC LIMIT 1", (turn_id,)).fetchone()
                    if lost_commit is not None:
                        lost_commit_id = strict_non_negative_int(lost_commit[0], field='storage_commits.commit_id')
                        lost_outcome = strict_text(lost_commit[1], field='storage_commits.outcome')
                        lost_status = strict_text(lost_commit[2], field='storage_commits.status')
                        execution_lost = lost_outcome == ControlOutcome.EXECUTION_LOST.value and lost_status == 'committed'
                        if execution_lost:
                            load_committed_storage_commit(connection, commit_id=lost_commit_id)
                    if not execution_lost:
                        raise ValueError('turn_not_resumable')
                try:
                    validate_turn_transition(current_status, TurnStatus.ACTIVE.value, explicit_resume=True, execution_lost=execution_lost)
                except ItemSchemaError as error:
                    raise ValueError('turn_not_resumable') from error
                execution_id = 'execution-' + uuid4().hex
                attempt = strict_non_negative_int(connection.execute('SELECT COALESCE(MAX(attempt), 0) + 1 FROM executions WHERE session_id = ? AND thread_id = ? AND turn_id = ?', (session_id, owner_thread_id, turn_id)).fetchone()[0], field='executions.attempt')
                previous = connection.execute('SELECT execution_id, job_id FROM executions WHERE session_id = ? AND thread_id = ? AND turn_id = ? ORDER BY attempt DESC LIMIT 1', (session_id, owner_thread_id, turn_id)).fetchone()
                if previous is None:
                    raise RuntimeError(f'可恢复 Turn 缺少前序 execution: {turn_id}')
                previous_execution_id = strict_text(previous[0], field='executions.execution_id')
                resumed_job_id = job_id
                accepted_ingress_id = strict_text(row[1], field='turn_records.accepted_ingress_id')
                acceptance = connection.execute('SELECT accepted_ingress_id, payload_hash FROM turn_acceptances WHERE session_id = ? AND thread_id = ? AND turn_id = ?', (session_id, owner_thread_id, turn_id)).fetchone()
                if acceptance is None or acceptance[0] != accepted_ingress_id:
                    raise RuntimeError(f'可恢复 Turn 缺少匹配 owner acceptance: {turn_id}')
                payload_hash = strict_text(acceptance[1], field='turn_acceptances.payload_hash')
                connection.execute('BEGIN IMMEDIATE')
                _commit_id, _commit_offset = self._storage._append_v2_records_transaction(connection, owner_thread_id, checkpoint_ns, (), session_id=session_id, commit_kind=CommitKind.TERMINAL_CONVERGENCE.value, commit_mode=CommitMode.METADATA_ONLY.value, outcome=ControlOutcome.UNKNOWN.value, subject_id=turn_id, idempotency_key=f'resume:{turn_id}:{execution_id}', metadata={'phase': 'resume_started', 'execution_id': execution_id, 'previous_execution_id': previous_execution_id}, begin_transaction=False)
                admission_key = f'resume:{turn_id}:{execution_id}'
                admission_hash = user_turn_admission_payload_hash(session_id=session_id, thread_id=owner_thread_id, turn_id=turn_id, execution_id=execution_id, job_id=resumed_job_id, accepted_ingress_id=accepted_ingress_id, admission_idempotency_key=admission_key, payload_hash=payload_hash)
                timestamp = _now()
                connection.execute("INSERT INTO executions(execution_id, session_id, thread_id, turn_id, execution_origin, job_id, admission_idempotency_key, admission_payload_hash, attempt, execution_ordinal, accepted_ingress_id, outcome, resumed_from_execution_id, replay_of_execution_id, created_at, updated_at) VALUES (?, ?, ?, ?, 'user_turn', ?, ?, ?, ?, ?, ?, 'unknown', ?, NULL, ?, ?)", (execution_id, session_id, owner_thread_id, turn_id, resumed_job_id, admission_key, admission_hash, attempt, attempt, accepted_ingress_id, previous_execution_id, timestamp, timestamp))
                connection.execute("INSERT INTO turn_execution_links(session_id, thread_id, turn_id, execution_id, execution_role, execution_ordinal, link_idempotency_key, created_at) VALUES (?, ?, ?, ?, 'resume', ?, ?, ?)", (session_id, owner_thread_id, turn_id, execution_id, attempt, f'{turn_id}:resume:{execution_id}', timestamp))
                connection.execute("UPDATE turn_records SET status = 'active', last_execution_id = ?, updated_at = ? WHERE session_id = ? AND thread_id = ? AND turn_id = ?", (execution_id, timestamp, session_id, owner_thread_id, turn_id))
                self._storage._commit_connection(connection)
                return {'turn_id': turn_id, 'execution_id': execution_id, 'job_id': resumed_job_id, 'admission_idempotency_key': admission_key, 'attempt': attempt}

    def mark_execution_lost(
        self,
        session_id: str,
        *,
        turn_id: str | None,
        thread_id: str,
        execution_id: str | None = None,
        checkpoint_ns: str = "",
        reason: str = "provider_result_commit_missing",
    ) -> dict[str, object]:
        """把 dispatch 后、terminal commit 前丢失的 execution 收敛为 unknown。"""
        session_id = strict_text(session_id, field="session_id")
        owner_thread_id = self._storage._resolve_thread_identity(session_id, strict_text(thread_id, field="thread_id"))
        turn_id = strict_optional_text(turn_id, field="turn_id")
        strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        strict_text(reason, field="execution lost reason")
        execution_id = strict_optional_text(execution_id, field="execution_id")
        if turn_id is None:
            if execution_id is None:
                raise ValueError("Turn-less execution lost 必须指定 execution_id")
            self.converge_execution(
                session_id,
                thread_id=owner_thread_id,
                turn_id=None,
                execution_id=execution_id,
                outcome=ControlOutcome.EXECUTION_LOST.value,
                turn_status=None,
                checkpoint_ns=checkpoint_ns,
            )
            return {
                "execution_id": execution_id,
                "status": ControlOutcome.EXECUTION_LOST.value,
                "outcome": ControlOutcome.EXECUTION_LOST.value,
                "reason": reason,
            }
        with self._storage._lock(owner_thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(session_id, checkpoint_ns, thread_id=owner_thread_id)
            with self._storage._connect(
                owner_thread_id, checkpoint_ns, session_id=session_id
            ) as connection:
                target_execution = execution_id or self.execution_for_turn(
                    session_id, thread_id=owner_thread_id,
                    turn_id=turn_id, checkpoint_ns=checkpoint_ns, writer_connection=connection,
                )
                admission = self.execution_admission(
                    session_id, thread_id=owner_thread_id,
                    execution_id=target_execution, checkpoint_ns=checkpoint_ns, writer_connection=connection,
                )
                if admission.turn_id != turn_id:
                    raise KeyError(f"execution 不存在或不属于 Turn: {target_execution}")
                turn = connection.execute(
                    "SELECT status FROM turn_records "
                    "WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
                    (session_id, owner_thread_id, turn_id),
                ).fetchone()
                if turn is None:
                    raise KeyError(f"Turn 不存在: {turn_id}")
                current_status = strict_text(turn[0], field="turn_records.status")
                execution_outcome = strict_text(
                    connection.execute(
                        "SELECT outcome FROM executions WHERE execution_id = ?",
                        (target_execution,),
                    ).fetchone()[0],
                    field="executions.outcome",
                )
            if current_status in {
                TurnStatus.COMPLETED.value, TurnStatus.COMPLETED_EMPTY.value,
                TurnStatus.FAILED.value, TurnStatus.CANCELLED.value,
                TurnStatus.INTERRUPTED.value,
            }:
                return {
                    "turn_id": turn_id, "execution_id": target_execution,
                    "status": current_status, "outcome": execution_outcome,
                }
            already_lost = (
                current_status == TurnStatus.UNKNOWN.value
                and execution_outcome == ControlOutcome.EXECUTION_LOST.value
            )
            commit_id = self.converge_execution(
                session_id, thread_id=owner_thread_id,
                turn_id=turn_id, execution_id=target_execution,
                outcome=ControlOutcome.EXECUTION_LOST.value,
                turn_status=TurnStatus.UNKNOWN.value, checkpoint_ns=checkpoint_ns,
                loss_reason=reason,
            )
            result = {
                "turn_id": turn_id, "execution_id": target_execution,
                "status": TurnStatus.UNKNOWN.value,
                "outcome": ControlOutcome.EXECUTION_LOST.value,
                "commit_id": commit_id,
            }
            if already_lost:
                result["idempotent"] = True
            return result


__all__ = ["RolloutExecutionRecoveryMixin"]
