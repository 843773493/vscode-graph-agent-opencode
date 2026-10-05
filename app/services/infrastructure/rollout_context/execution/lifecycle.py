"""Rollout v2 Turn/execution/model-call 持久化 owner。

这些 mixin 只依赖 RolloutStorage 提供的 SQLite、JSONL transaction 和 domain
ports；它们不承担 LangChain/provider projection，也不读取 v1 数据。
"""

from __future__ import annotations

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    CommitKind,
    CommitMode,
    ControlOutcome,
    SemanticKind,
    TurnStatus,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.validation import (
    validate_turn_transition,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

_V2_TO_HISTORY_STATUS = {
    TurnStatus.OPEN.value: "accepted",
    TurnStatus.ACTIVE.value: "running",
    TurnStatus.COMPLETED.value: "completed",
    TurnStatus.COMPLETED_EMPTY.value: "completed",
    TurnStatus.INTERRUPTED.value: "timed_out",
    TurnStatus.CANCELLED.value: "cancelled",
    TurnStatus.FAILED.value: "failed",
    TurnStatus.UNKNOWN.value: "failed",
}


class RolloutTurnLifecycleMixin:
    """Turn finalization/cancel/status transition owner。"""

    def append_turn_finalize(
        self,
        *,
        session_id: str,
        thread_id: str | None = None,
        checkpoint_ns: str = "",
        turn_id: str,
        final_message_sequence: int,
        final_message_id: str,
    ) -> None:
        """在 v2 TurnRecord 上完成 terminal convergence。

        message projection 只用于解析外部 message id；Turn、final item、execution、
        assembly 和 storage commit 的权威状态全部来自 v2 表，v1 rollout 不进入此入口。
        """
        strict_text(turn_id, field="turn_id")
        strict_text(final_message_id, field="final_message_id")
        strict_non_negative_int(
            final_message_sequence,
            field="final_message_sequence",
        )
        owner_thread_id = self._resolve_thread_identity(session_id, thread_id)
        with self._lock(
            owner_thread_id, checkpoint_ns, session_id=session_id
        ):
            self.initialize(session_id, checkpoint_ns, thread_id=owner_thread_id)
            with self._connect(
                owner_thread_id, checkpoint_ns, session_id=session_id
            ) as connection:
                self._require_v2_runtime(connection)
                row = connection.execute(
                    "SELECT message_id FROM messages WHERE message_sequence = ?",
                    (final_message_sequence,),
                ).fetchone()
                if row is None or strict_text(row[0], field="messages.message_id") != final_message_id:
                    raise RuntimeError(
                        "turn_finalize 指向不存在或 ID 不匹配的消息 projection"
                    )
                item_row = connection.execute(
                    "SELECT item_id, status, semantic_kind, turn_id "
                    "FROM item_catalog WHERE item_id = ?",
                    (f"item-{final_message_id}",),
                ).fetchone()
                if item_row is None:
                    raise RuntimeError(
                        "turn_finalize 的 message projection 没有对应 canonical item: "
                        f"{final_message_id}"
                    )
                turn_row = connection.execute(
                    "SELECT status, final_item_id FROM turn_records WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if turn_row is None:
                    raise KeyError(f"v2 Turn 不存在: {turn_id}")
                current_turn_status = strict_text(
                    turn_row[0], field="turn_records.status"
                )
                final_item_id = strict_text(
                    item_row[0], field="item_catalog.item_id"
                )
                existing_final_item_id = strict_optional_text(
                    turn_row[1], field="turn_records.final_item_id"
                )
                if current_turn_status == TurnStatus.COMPLETED.value:
                    if existing_final_item_id == final_item_id:
                        return
                    raise ValueError("completed Turn 的 final_item_id 不可修改")
                try:
                    validate_turn_transition(
                        current_turn_status, TurnStatus.COMPLETED.value
                    )
                except ItemSchemaError as error:
                    raise ValueError(str(error)) from error
                if (
                    item_row[1] != CanonicalItemStatus.COMPLETED.value
                    or item_row[2] != SemanticKind.ASSISTANT_OUTPUT.value
                    or item_row[3] != turn_id
                ):
                    raise ValueError(
                        "final_item_id 必须指向同一 Turn 的 completed assistant_output item"
                    )
                latest_execution = connection.execute(
                    "SELECT execution_id FROM executions WHERE turn_id = ? "
                    "ORDER BY attempt DESC, execution_ordinal DESC LIMIT 1",
                    (turn_id,),
                ).fetchone()
                if latest_execution is None:
                    raise RuntimeError(f"Turn 没有可收敛的 execution: {turn_id}")
                latest_execution_id = strict_text(
                    latest_execution[0], field="executions.execution_id"
                )
                timestamp = _now()
                connection.execute("BEGIN IMMEDIATE")
                commit_id, _commit_offset = self._append_v2_records_transaction(
                    connection,
                    owner_thread_id,
                    checkpoint_ns,
                    (),
                    session_id=session_id,
                    commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                    commit_mode=CommitMode.METADATA_ONLY.value,
                    outcome=ControlOutcome.COMPLETED.value,
                    subject_id=turn_id,
                    idempotency_key=(
                        f"terminal:{turn_id}:{latest_execution_id}:{item_row[0]}"
                    ),
                    begin_transaction=False,
                )
                connection.execute(
                    "UPDATE turn_records SET status = 'completed', final_item_id = ?, "
                    "last_execution_id = ?, updated_at = ? WHERE turn_id = ?",
                    (final_item_id, latest_execution_id, timestamp, turn_id),
                )
                connection.execute(
                    "UPDATE executions SET outcome = 'completed', updated_at = ? "
                    "WHERE turn_id = ? AND execution_id = ?",
                    (timestamp, turn_id, latest_execution_id),
                )
                connection.execute(
                    "UPDATE model_calls SET outcome = 'completed', "
                    "dispatch_state = 'completed' WHERE execution_id = ?",
                    (latest_execution_id,),
                )
                connection.execute(
                    "UPDATE context_assemblies SET status = 'terminal', "
                    "outcome = 'completed', terminal_at = ? "
                    "WHERE turn_id = ? AND execution_id = ? AND status = 'sealed'",
                    (timestamp, turn_id, latest_execution_id),
                )
                connection.execute(
                    "UPDATE item_projections SET phase = 'final_answer', updated_at = ? "
                    "WHERE item_id = ?",
                    (timestamp, final_item_id),
                )
                # `turns`/`context_view_turns` 是旧消息坐标的派生索引，不是
                # v2 lifecycle authority；同步它们是为了让已有 message-range
                # reader 与 v2 TurnRecord 在同一事务后观察到同一 terminal pointer。
                connection.execute(
                    "UPDATE turns SET final_message_sequence = ?, final_message_id = ?, "
                    "status = 'completed', updated_at = ? WHERE turn_id = ?",
                    (
                        final_message_sequence,
                        final_message_id,
                        timestamp,
                        turn_id,
                    ),
                )
                connection.execute(
                    "UPDATE context_view_turns SET final_message_sequence = ? WHERE turn_id = ?",
                    (final_message_sequence, turn_id),
                )
                self._insert_control(
                    connection,
                    "turn_finalized",
                    "turn",
                    turn_id,
                    None,
                    None,
                    None,
                    {
                        "final_item_id": final_item_id,
                        "execution_id": latest_execution_id,
                        "outcome": ControlOutcome.COMPLETED.value,
                    },
                    f"terminal-control:{commit_id}",
                    timestamp,
                )
                self._commit_connection(connection)

    def cancel_unfinished_turns(
        self,
        *,
        session_id: str,
        thread_id: str | None = None,
        checkpoint_ns: str = "",
    ) -> int:
        """取消 fork 后不会由子会话继续执行的未完成 Turn。

        fork 只复制 checkpoint 内容，不复制源会话的运行时 Job。若目标 rollout
        仍保留 running/streaming 等状态，前端会在没有任何活动 Job 的情况下永久
        显示“正在处理”。没有最终消息指针的 Turn 在子会话中只能明确标记为取消，
        等用户需要时再通过重试创建新的 Job。
        """
        owner_thread_id = self._resolve_thread_identity(session_id, thread_id)
        with self._lock(
            owner_thread_id, checkpoint_ns, session_id=session_id
        ):
            self.initialize(session_id, checkpoint_ns, thread_id=owner_thread_id)
            with self._connect(
                owner_thread_id, checkpoint_ns, session_id=session_id
            ) as connection:
                self._require_v2_runtime(connection)
                rows = connection.execute(
                    "SELECT turn_id, status, last_execution_id "
                    "FROM turn_records "
                    "WHERE status IN (?, ?) AND final_item_id IS NULL "
                    "ORDER BY turn_ordinal",
                    (TurnStatus.OPEN.value, TurnStatus.ACTIVE.value),
                ).fetchall()
                if not rows:
                    return 0
                timestamp = _now()
                execution_ids: list[str] = []
                for turn_id_value, _status, last_execution_id in rows:
                    turn_id = strict_text(turn_id_value, field="turn_records.turn_id")
                    execution_id = last_execution_id
                    if execution_id is None:
                        execution_row = connection.execute(
                            "SELECT execution_id FROM executions WHERE turn_id = ? "
                            "ORDER BY attempt DESC, execution_ordinal DESC LIMIT 1",
                            (turn_id,),
                        ).fetchone()
                        execution_id = execution_row[0] if execution_row else None
                    if execution_id is None:
                        raise RuntimeError(
                            f"取消 Turn 缺少 execution，拒绝静默收敛: {turn_id}"
                        )
                    execution_ids.append(
                        strict_text(execution_id, field="executions.execution_id")
                    )
                connection.execute("BEGIN IMMEDIATE")
                commit_id, _commit_offset = self._append_v2_records_transaction(
                    connection,
                    owner_thread_id,
                    checkpoint_ns,
                    (),
                    session_id=session_id,
                    commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                    commit_mode=CommitMode.METADATA_ONLY.value,
                    outcome=ControlOutcome.CANCELLED.value,
                    subject_id=thread_id,
                    idempotency_key=(
                        "cancel-unfinished:"
                        + thread_id
                        + ":"
                        + ",".join(
                            strict_text(row[0], field="turn_records.turn_id")
                            for row in rows
                        )
                    ),
                    begin_transaction=False,
                )
                transaction_id = f"cancel-control:{commit_id}"
                for row, execution_id in zip(rows, execution_ids, strict=True):
                    turn_id = strict_text(row[0], field="turn_records.turn_id")
                    validate_turn_transition(
                        strict_text(row[1], field="turn_records.status"),
                        TurnStatus.CANCELLED.value,
                    )
                    connection.execute(
                        "UPDATE turn_records SET status = ?, last_execution_id = ?, "
                        "updated_at = ? WHERE turn_id = ?",
                        (
                            TurnStatus.CANCELLED.value,
                            execution_id,
                            timestamp,
                            turn_id,
                        ),
                    )
                    connection.execute(
                        "UPDATE executions SET outcome = ?, updated_at = ? WHERE execution_id = ?",
                        (ControlOutcome.CANCELLED.value, timestamp, execution_id),
                    )
                    connection.execute(
                        "UPDATE model_calls SET outcome = ?, dispatch_state = 'unknown' "
                        "WHERE execution_id = ? AND outcome IS NULL",
                        (ControlOutcome.CANCELLED.value, execution_id),
                    )
                    connection.execute(
                        "UPDATE context_assemblies SET status = 'terminal', outcome = ?, "
                        "terminal_at = ? WHERE turn_id = ? AND execution_id = ? "
                        "AND status = 'sealed'",
                        (
                            ControlOutcome.CANCELLED.value,
                            timestamp,
                            turn_id,
                            execution_id,
                        ),
                    )
                    # 旧 message 坐标仅是派生 UI 索引；v2 TurnRecord 已经是唯一
                    # lifecycle authority，两个索引在同一事务内同步以避免瞬时矛盾。
                    connection.execute(
                        "UPDATE turns SET status = 'cancelled', updated_at = ? "
                        "WHERE turn_id = ? AND final_message_sequence IS NULL",
                        (timestamp, turn_id),
                    )
                    self._insert_control(
                        connection,
                        "turn_status",
                        "turn",
                        turn_id,
                        None,
                        None,
                        None,
                        {
                            "status": TurnStatus.CANCELLED.value,
                            "outcome": ControlOutcome.CANCELLED.value,
                            "reason": "fork_runtime_not_copied",
                            "execution_id": execution_id,
                        },
                        transaction_id,
                        timestamp,
                    )
                last_control = connection.execute(
                    "SELECT control_sequence FROM control_events WHERE transaction_id = ? "
                    "ORDER BY control_sequence DESC LIMIT 1",
                    (transaction_id,),
                ).fetchone()
                if last_control is None:
                    raise RuntimeError(
                        "cancel unfinished turns 未生成 control event"
                    )
                connection.execute(
                    "UPDATE database_meta SET last_control_sequence = ?, updated_at = ? "
                    "WHERE singleton_id = 1",
                    (
                        strict_non_negative_int(
                            last_control[0],
                            field="control_events.control_sequence",
                        ),
                        timestamp,
                    ),
                )
                self._commit_connection(connection)
                return len(rows)

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
        try:
            execution_id = self.execution_for_turn(
                session_id, thread_id=thread_id, turn_id=turn_id,
                checkpoint_ns=checkpoint_ns,
            )
        except KeyError:
            return False
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
        with self._lock(
            admission.thread_id, checkpoint_ns, session_id=admission.session_id,
        ):
            with self._connect(
                admission.thread_id, checkpoint_ns, session_id=admission.session_id,
            ) as connection:
                self._require_v2_runtime(connection)
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

    def final_message_sequence(
        self,
        *,
        session_id: str,
        thread_id: str | None = None,
        checkpoint_ns: str,
        turn_id: str,
        final_message_id: str,
    ) -> int:
        """解析已提交 final message 的物理序号，供受控 writer 使用。"""
        owner_thread_id = self._resolve_thread_identity(session_id, thread_id)
        self.initialize(session_id, checkpoint_ns, thread_id=owner_thread_id)
        with self._connect(
            owner_thread_id, checkpoint_ns, session_id=session_id
        ) as connection:
            self._require_v2_runtime(connection)
            row = connection.execute(
                "SELECT message_sequence FROM messages WHERE turn_id = ? AND message_id = ? ORDER BY message_sequence DESC LIMIT 1",
                (turn_id, final_message_id),
            ).fetchone()
        if row is None:
            raise RuntimeError(
                "最终 assistant 消息未出现在 rollout projection 中: "
                f"session_id={session_id}, turn_id={turn_id}, message_id={final_message_id}"
            )
        return strict_non_negative_int(
            row[0], field="messages.message_sequence"
        )
