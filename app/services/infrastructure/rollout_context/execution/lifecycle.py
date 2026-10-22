"""Rollout v2 Turn/execution/model-call 持久化 owner。

这些 mixin 只依赖 RolloutContextStore 提供的 SQLite、JSONL transaction 和 domain
ports；它们不承担 LangChain/provider projection，也不读取 v1 数据。
"""

from __future__ import annotations

from app.domain.itemized.enums import (
    ControlOutcome,
    TurnStatus,
)
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_text,
)


class RolloutTurnLifecycleMixin:
    """Turn/Job 控制终态的准入与收敛 owner。"""

    def mark_job_terminal_status(
        self,
        *,
        session_id: str,
        thread_id: str,
        job_id: str,
        status: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """只收口已提交的精确 Job admission，user/internal 共用终态事务。"""
        try:
            admission = self.execution_admission_for_job(
                session_id, thread_id=thread_id, job_id=job_id,
                checkpoint_ns=checkpoint_ns,
            )
        except KeyError:
            return False
        return self._mark_admission_terminal_status(
            admission, status=status, checkpoint_ns=checkpoint_ns,
            preserve_recovery=True,
        )

    def mark_turn_terminal_status(
        self,
        *,
        session_id: str,
        thread_id: str,
        turn_id: str,
        status: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """独立 Turn 控制也通过真实 execution 委派唯一 convergence 事务。"""
        session_id = strict_text(session_id, field="session_id")
        thread_id = self._storage.fork_paths.thread_identity(
            session_id, strict_text(thread_id, field="thread_id")
        )
        turn_id = strict_text(turn_id, field="turn_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True,
        )
        self._storage.initialize(session_id, checkpoint_ns, thread_id=thread_id)
        with self._storage._connect(
            thread_id, checkpoint_ns, session_id=session_id, read_only=True,
        ) as connection:
            self._storage._require_v2_runtime(connection)
            execution = connection.execute(
                "SELECT COALESCE(last_execution_id, initial_execution_id) "
                "FROM turn_records WHERE session_id = ? AND thread_id = ? "
                "AND turn_id = ?",
                (session_id, thread_id, turn_id),
            ).fetchone()
        if execution is None:
            return False
        # 终态吸收也须读取同一已提交执行；dispatch 的运行态 guard 不用于收敛。
        execution_id = strict_text(
            execution[0], field="turn_records.current_execution_id",
        )
        admission = self.execution_admission(
            session_id, thread_id=thread_id, execution_id=execution_id,
            checkpoint_ns=checkpoint_ns,
        )
        return self._mark_admission_terminal_status(
            admission, status=status, checkpoint_ns=checkpoint_ns,
            preserve_recovery=False,
        )

    def _mark_admission_terminal_status(
        self,
        admission: ExecutionAdmission,
        *,
        status: str,
        checkpoint_ns: str,
        preserve_recovery: bool,
    ) -> bool:
        outcomes = {
            "failed": ControlOutcome.FAILED.value,
            "cancelled": ControlOutcome.CANCELLED.value,
            "timed_out": ControlOutcome.INTERRUPTED.value,
            "execution_lost": ControlOutcome.EXECUTION_LOST.value,
        }
        if status not in outcomes:
            raise ValueError(f"未知 execution terminal status: {status!r}")
        outcome = outcomes[status]
        turn_status = (
            TurnStatus.UNKNOWN.value if status == "execution_lost" else outcome
        ) if admission.turn_id is not None else None
        with self._storage._lock(
            admission.thread_id, checkpoint_ns, session_id=admission.session_id,
        ):
            with self._storage._connect(
                admission.thread_id, checkpoint_ns, session_id=admission.session_id,
            ) as connection:
                self._storage._require_v2_runtime(connection)
                execution = connection.execute(
                    "SELECT outcome FROM executions WHERE session_id = ? "
                    "AND thread_id = ? AND execution_id = ?",
                    (admission.session_id, admission.thread_id, admission.execution_id),
                ).fetchone()
                if execution is None:
                    raise KeyError(f"execution admission 已丢失: {admission.execution_id}")
                current_outcome = strict_text(execution[0], field="executions.outcome")
                if current_outcome not in {
                    ControlOutcome.UNKNOWN.value, ControlOutcome.EXECUTION_LOST.value,
                }:
                    return True
                # Job failed 是业务轴；不覆盖已丢失/未知 execution 的恢复语义。
                if preserve_recovery and status == "failed":
                    return True
                if admission.turn_id is not None:
                    turn = connection.execute(
                        "SELECT status FROM turn_records WHERE session_id = ? "
                        "AND thread_id = ? AND turn_id = ?",
                        (admission.session_id, admission.thread_id, admission.turn_id),
                    ).fetchone()
                    if turn is None:
                        raise KeyError(f"admission 的 Turn 已丢失: {admission.turn_id}")
                    current_status = strict_text(turn[0], field="turn_records.status")
                    if current_status not in {
                        TurnStatus.OPEN.value, TurnStatus.ACTIVE.value,
                        TurnStatus.UNKNOWN.value,
                    } or (
                        current_status == TurnStatus.UNKNOWN.value and status == "failed"
                    ):
                        return True
                if current_outcome == ControlOutcome.EXECUTION_LOST.value and status == "execution_lost":
                    return True
            self.converge_execution(
                admission.session_id, thread_id=admission.thread_id,
                turn_id=admission.turn_id, execution_id=admission.execution_id,
                outcome=outcome, turn_status=turn_status, checkpoint_ns=checkpoint_ns,
                loss_reason=(
                    "execution_lost"
                    if status == "execution_lost" and admission.turn_id is not None
                    else None
                ),
            )
            return True
