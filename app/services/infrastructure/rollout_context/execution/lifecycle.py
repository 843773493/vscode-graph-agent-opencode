"""Rollout v2 Turn/execution/model-call 持久化 owner。

这些 mixin 只依赖 RolloutStorage 提供的 SQLite、JSONL transaction 和 domain
ports；它们不承担 LangChain/provider projection，也不读取 v1 数据。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    CommitKind,
    CommitMode,
    ControlOutcome,
    SemanticKind,
    TurnStatus,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.validation import (
    is_terminal_turn_status,
    validate_turn_transition,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
        thread_id: str,
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
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
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
                    thread_id,
                    checkpoint_ns,
                    (),
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
                    "UPDATE executions SET outcome = 'completed' "
                    "WHERE turn_id = ? AND execution_id = ?",
                    (turn_id, latest_execution_id),
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
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> int:
        """取消 fork 后不会由子会话继续执行的未完成 Turn。

        fork 只复制 checkpoint 内容，不复制源会话的运行时 Job。若目标 rollout
        仍保留 running/streaming 等状态，前端会在没有任何活动 Job 的情况下永久
        显示“正在处理”。没有最终消息指针的 Turn 在子会话中只能明确标记为取消，
        等用户需要时再通过重试创建新的 Job。
        """
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
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
                    thread_id,
                    checkpoint_ns,
                    (),
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
                        "UPDATE executions SET outcome = ? WHERE execution_id = ?",
                        (ControlOutcome.CANCELLED.value, execution_id),
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

    def mark_turn_terminal_status(
        self,
        *,
        thread_id: str,
        turn_id: str,
        status: str,
        checkpoint_ns: str = "",
    ) -> bool:
        """把失败或取消等终态写入 SQLite，供历史恢复继续显示可重试状态。

        不是所有 Job 都对应聊天 Turn（例如后台任务），因此找不到 Turn 时
        返回 ``False``。找到后状态和控制事件在同一事务中提交，避免页面切换
        后只能从短生命周期的 JobService 内存状态恢复失败信息。
        """
        if status not in {"failed", "cancelled", "timed_out"}:
            raise ValueError(f"非法的 Turn 终态: {status}")
        if not self.index_path(thread_id, checkpoint_ns).is_file():
            # 后台 Job 也会进入统一终态事件流，但它们未必拥有聊天 Turn。
            return False
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                itemized_turn = connection.execute(
                    "SELECT status FROM turn_records WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if itemized_turn is not None:
                    outcome = {
                        "failed": ControlOutcome.FAILED.value,
                        "cancelled": ControlOutcome.CANCELLED.value,
                        "timed_out": ControlOutcome.INTERRUPTED.value,
                    }[status]
                    target_status = {
                        "failed": TurnStatus.FAILED.value,
                        "cancelled": TurnStatus.CANCELLED.value,
                        "timed_out": TurnStatus.INTERRUPTED.value,
                    }[status]
                    current_status = strict_text(
                        itemized_turn[0], field="turn_records.status"
                    )
                    if current_status == target_status:
                        return True
                    # AgentExecutionService 在异常收尾时先将丢失的
                    # execution/Turn 收敛为 unknown；JobService 随后仍会
                    # 报告业务 Job failed。两者是不同状态轴，不能把
                    # execution_lost 的可恢复 Turn 强行改写成 failed。
                    if (
                        current_status == TurnStatus.UNKNOWN.value
                        and status == "failed"
                    ):
                        return True
                    # Turn 已经收敛到与本请求不同的终态时，它仍是同一个终态轴；
                    # 例如 Turn 正常 completed 后迟到的 job_failed 事件。
                    # 启动恢复与事件监听都是 best-effort 收敛路径，不能因为
                    # 已有终态而抛错，否则每次启动都会用同一条历史 Trace 再次
                    # 崩溃并写下新的失败事件，形成无法自愈的启动死循环。
                    if is_terminal_turn_status(current_status):
                        return True
                    try:
                        validate_turn_transition(current_status, target_status)
                    except ItemSchemaError as error:
                        raise ValueError(
                            f"非法 Turn.status 转移: {current_status}->{target_status}"
                        ) from error
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
                    connection.execute("BEGIN IMMEDIATE")
                    self._append_v2_records_transaction(
                        connection,
                        thread_id,
                        checkpoint_ns,
                        (),
                        commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                        commit_mode=CommitMode.METADATA_ONLY.value,
                        outcome=outcome,
                        subject_id=turn_id,
                        idempotency_key=(
                            f"terminal:{turn_id}:{latest_execution_id}:{outcome}"
                        ),
                        begin_transaction=False,
                    )
                    timestamp = _now()
                    connection.execute(
                        "UPDATE turn_records SET status = ?, updated_at = ? WHERE turn_id = ?",
                        (
                            target_status,
                            timestamp,
                            turn_id,
                        ),
                    )
                    connection.execute(
                        "UPDATE executions SET outcome = ? WHERE turn_id = ? AND execution_id = ?",
                        (outcome, turn_id, latest_execution_id),
                    )
                    connection.execute(
                        "UPDATE model_calls SET outcome = ?, dispatch_state = CASE WHEN ? = 'failed' THEN 'failed' ELSE 'unknown' END WHERE execution_id = ?",
                        (outcome, outcome, latest_execution_id),
                    )
                    connection.execute(
                        "UPDATE context_assemblies SET status = 'terminal', outcome = ?, terminal_at = ? WHERE turn_id = ? AND execution_id = ? AND status = 'sealed'",
                        (outcome, timestamp, turn_id, latest_execution_id),
                    )
                    self._commit_connection(connection)
                    return True
                return False

    def final_message_sequence(
        self,
        *,
        thread_id: str,
        checkpoint_ns: str,
        turn_id: str,
        final_message_id: str,
    ) -> int:
        """解析已提交 final message 的物理序号，供受控 writer 使用。"""
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns) as connection:
            self._require_v2_runtime(connection)
            row = connection.execute(
                "SELECT message_sequence FROM messages WHERE turn_id = ? AND message_id = ? ORDER BY message_sequence DESC LIMIT 1",
                (turn_id, final_message_id),
            ).fetchone()
        if row is None:
            raise RuntimeError(
                "最终 assistant 消息未出现在 rollout projection 中: "
                f"session_id={thread_id}, turn_id={turn_id}, message_id={final_message_id}"
            )
        return strict_non_negative_int(
            row[0], field="messages.message_sequence"
        )
