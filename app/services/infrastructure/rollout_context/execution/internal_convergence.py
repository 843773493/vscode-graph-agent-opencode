"""Rollout v2 Turn/execution/model-call 持久化 owner。

这些 mixin 只依赖 RolloutContextStore 提供的 SQLite、JSONL transaction 和 domain
ports；它们不承担 LangChain/provider projection，也不读取 v1 数据。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import (
    CommitKind,
    CommitMode,
    ControlOutcome,
)
from app.domain.itemized.errors import InternalExecutionInterruptedError
from app.domain.itemized.records import (
    CanonicalItemRecord,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_idempotency_commit,
    read_committed_item_identities,
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutInternalConvergenceMixin:
    """Turn-less execution 与共享 ModelCall/assembly 的原子终态收敛。"""

    def _terminal_model_call_id(
        self, connection, *, session_id: str, thread_id: str,
        execution_id: str, outcome: str,
    ) -> str | None:
        """校验最新调用；执行成功不能覆盖调用失败，执行中止可晚于调用完成。"""
        model_call = connection.execute(
            "SELECT model_call_id, outcome FROM model_calls "
            "WHERE session_id=? AND thread_id=? AND execution_id=? "
            "ORDER BY attempt DESC LIMIT 1",
            (session_id, thread_id, execution_id),
        ).fetchone()
        if model_call is None:
            return None
        model_call_id = strict_text(model_call[0], field="model_calls.model_call_id")
        model_outcome = strict_text(model_call[1], field="model_calls.outcome")
        if model_outcome not in {value.value for value in ControlOutcome}:
            raise ValueError(f"model_call outcome 非法: {model_call_id}")
        if outcome in {
            ControlOutcome.COMPLETED.value, ControlOutcome.COMPLETED_EMPTY.value,
        } and model_outcome not in {ControlOutcome.UNKNOWN.value, outcome}:
            raise ValueError(
                "terminal convergence 与最新 model_call outcome 冲突: "
                f"model_call={model_call_id}, existing={model_outcome}, requested={outcome}"
            )
        return model_call_id

    def _converge_execution_calls_and_assemblies(
        self, connection, *, session_id: str, thread_id: str,
        execution_id: str, latest_model_call_id: str | None,
        outcome: str, timestamp: str, control_correction: bool,
    ) -> None:
        """同事务闭合待定调用与 assembly，保留每次 Provider 调用的既有结果。

        assembly 的 outcome 沿其已登记 ModelCall；尚无调用或调用结果未知时，
        记录 execution 如何结束该封存请求。调用完成可以早于工具及执行完成。
        """
        if latest_model_call_id is not None:
            model_outcome = (
                ControlOutcome.UNKNOWN.value
                if outcome == ControlOutcome.EXECUTION_LOST.value else outcome
            )
            updated = connection.execute(
                "UPDATE model_calls SET outcome=CASE WHEN outcome='unknown' THEN ? "
                "ELSE outcome END, dispatch_state=CASE WHEN outcome!='unknown' "
                "THEN dispatch_state WHEN ? IN ('completed','completed_empty') "
                "THEN 'completed' WHEN ? IN ('failed','cancelled','interrupted') "
                "THEN 'failed' ELSE 'unknown' END "
                "WHERE session_id=? AND thread_id=? AND execution_id=? AND model_call_id=?",
                (model_outcome, model_outcome, model_outcome, session_id,
                 thread_id, execution_id, latest_model_call_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("terminal convergence latest model-call outcome 更新失败")
        assemblies = connection.execute(
            "SELECT a.assembly_id, a.model_call_id, m.outcome "
            "FROM context_assemblies a LEFT JOIN model_calls m "
            "ON m.session_id=a.session_id AND m.thread_id=a.thread_id "
            "AND m.execution_id=a.execution_id AND m.model_call_id=a.model_call_id "
            "WHERE a.session_id=? AND a.thread_id=? AND a.execution_id=? "
            "AND (a.status='sealed' OR (? AND a.status='terminal' "
            "AND a.outcome IN ('unknown','execution_lost')))",
            (session_id, thread_id, execution_id, control_correction),
        ).fetchall()
        for assembly_id, model_call_id, model_outcome in assemblies:
            # Provider fallback 可以复用同一封存 assembly；最终收敛时按其
            # 实际调用闭合。未登记响应事实的封存请求随 execution 收口。
            terminal_outcome = (
                outcome if model_outcome in {None, ControlOutcome.UNKNOWN.value}
                else strict_text(model_outcome, field=f"assembly model_call outcome: {assembly_id}")
            )
            if terminal_outcome not in {value.value for value in ControlOutcome}:
                raise ValueError(f"assembly model_call outcome 非法: {assembly_id}")
            updated = connection.execute(
                "UPDATE context_assemblies SET status='terminal', outcome=?, terminal_at=? "
                "WHERE session_id=? AND thread_id=? AND execution_id=? AND assembly_id=?",
                (terminal_outcome, timestamp, session_id, thread_id, execution_id, assembly_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"terminal convergence assembly outcome 更新失败: {assembly_id}")


    def _converge_internal_execution(
        self,
        connection,
        *,
        session_id: str,
        thread_id: str,
        execution_id: str,
        outcome: str,
        items: Sequence[CanonicalItemRecord],
        assembly_id: str | None,
        checkpoint_ns: str,
    ) -> int:
        if outcome == ControlOutcome.COMPLETED_EMPTY.value and items:
            raise ValueError("completed_empty internal execution 不得包含 canonical item")
        if outcome == ControlOutcome.COMPLETED.value and not items:
            raise ValueError("completed internal execution 必须包含 canonical output item")
        for item in items:
            if item.turn_id is not None or item.turn_scope is not None:
                raise ValueError(
                    "internal terminal item 不得归属 Turn 或 Turn scope: "
                    f"item={item.item_id}"
                )

        execution = connection.execute(
            "SELECT outcome, execution_origin, turn_id FROM executions "
            "WHERE session_id = ? AND thread_id = ? AND execution_id = ?",
            (session_id, thread_id, execution_id),
        ).fetchone()
        if execution is None:
            raise KeyError(f"internal execution 不存在: {execution_id}")
        if (
            strict_text(execution[1], field="executions.execution_origin") != "internal"
            or strict_optional_text(execution[2], field="executions.turn_id") is not None
        ):
            raise ValueError(
                f"internal terminal convergence 只接受 Turn-less internal execution: {execution_id}"
            )
        current_outcome = strict_text(execution[0], field="executions.outcome")
        admission = self.execution_admission(
            session_id, thread_id=thread_id, execution_id=execution_id,
            checkpoint_ns=checkpoint_ns, writer_connection=connection,
        )
        interrupt = self.internal_execution_interrupt(admission, connection=connection)
        if interrupt is not None and outcome in {ControlOutcome.COMPLETED.value, ControlOutcome.COMPLETED_EMPTY.value}:
            raise InternalExecutionInterruptedError(interrupt.interrupt_request_id)
        terminal_metadata = (
            {"interrupt_request_id": interrupt.interrupt_request_id, "after_interrupt_requested": True}
            if interrupt is not None else {}
        )
        control_correction = (
            current_outcome == ControlOutcome.EXECUTION_LOST.value
            and outcome in {ControlOutcome.CANCELLED.value, ControlOutcome.INTERRUPTED.value}
        )
        if current_outcome not in {ControlOutcome.UNKNOWN.value, outcome} and not control_correction:
            raise ValueError(
                "internal execution outcome 已收敛且与 terminal outcome 冲突: "
                f"execution={execution_id}, existing={current_outcome}, requested={outcome}"
            )

        idempotency_key = f"internal-terminal:{execution_id}:{outcome}"
        commit_mode = (
            CommitMode.ITEM_BEARING.value
            if items
            else CommitMode.METADATA_ONLY.value
        )
        committed = load_committed_idempotency_commit(
            connection,
            commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
            subject_id=execution_id,
            idempotency_key=idempotency_key,
            commit_mode=commit_mode,
            outcome=outcome,
            metadata=terminal_metadata,
            item_count=len(items),
        )
        if committed is not None:
            committed_items = tuple(
                (item_id, content_hash)
                for item_id, content_hash, _sequence in read_committed_item_identities(
                    connection,
                    commit_id=committed.commit_id,
                )
            )
            requested_items = tuple(
                (item.item_id, item.content_hash) for item in items
            )
            if committed_items != requested_items:
                raise ValueError(
                    "internal terminal 幂等键冲突：item identity/content 不一致"
                )
            return committed.commit_id
        if control_correction:
            lost_commit = load_committed_idempotency_commit(
                connection,
                commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                subject_id=execution_id,
                idempotency_key=f"internal-terminal:{execution_id}:execution_lost",
                commit_mode=CommitMode.METADATA_ONLY.value,
                outcome=ControlOutcome.EXECUTION_LOST.value,
                metadata=terminal_metadata,
                item_count=0,
            )
            if lost_commit is None:
                raise RuntimeError("internal execution_lost commit 缺失，禁止控制修正")
        if current_outcome != ControlOutcome.UNKNOWN.value and not control_correction:
            raise RuntimeError(
                "internal execution 已终态但 terminal convergence commit 缺失: "
                f"execution_id={execution_id}, outcome={current_outcome}"
            )

        if assembly_id is not None:
            assembly = connection.execute(
                "SELECT session_id, thread_id, turn_id, execution_id, status, outcome "
                "FROM context_assemblies WHERE assembly_id = ?",
                (assembly_id,),
            ).fetchone()
            if assembly is None:
                raise KeyError(f"sealed assembly 不存在: {assembly_id}")
            if (
                strict_text(assembly[0], field="context_assemblies.session_id")
                != session_id
                or strict_text(assembly[1], field="context_assemblies.thread_id")
                != thread_id
                or strict_optional_text(
                    assembly[2], field="context_assemblies.turn_id"
                )
                is not None
                or strict_text(
                    assembly[3], field="context_assemblies.execution_id"
                )
                != execution_id
                or strict_text(assembly[4], field="context_assemblies.status")
                != "sealed"
                or strict_optional_text(
                    assembly[5], field="context_assemblies.outcome"
                )
                not in {None, outcome}
            ):
                raise ValueError("internal terminal convergence assembly 关联冲突")

        model_call_id = self._terminal_model_call_id(
            connection, session_id=session_id, thread_id=thread_id,
            execution_id=execution_id, outcome=outcome,
        )

        last_item_row = connection.execute(
            "SELECT last_item_sequence FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if last_item_row is None:
            raise RuntimeError("internal terminal convergence 缺少 last_item_sequence")
        last_item_sequence = strict_non_negative_int(
            last_item_row[0], field="database_meta.last_item_sequence"
        )
        converged_items = tuple(
            replace(item, item_sequence=last_item_sequence + index + 1)
            for index, item in enumerate(items)
        )
        commit_id, _offset = self._storage._append_v2_records_transaction(
            connection,
            thread_id,
            checkpoint_ns,
            converged_items,
            session_id=session_id,
            commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
            commit_mode=commit_mode,
            outcome=outcome,
            subject_id=execution_id,
            idempotency_key=idempotency_key,
            metadata=terminal_metadata,
            begin_transaction=False,
        )
        for item in converged_items:
            self._storage._bind_canonical_item_execution(
                connection,
                item_id=item.item_id,
                execution_id=execution_id,
            )

        message_meta = connection.execute(
            "SELECT last_message_sequence FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if message_meta is None:
            raise RuntimeError(
                "internal terminal convergence 缺少 message sequence 元数据"
            )
        previous_last_message_sequence = strict_non_negative_int(
            message_meta[0], field="database_meta.last_message_sequence"
        )
        next_message_sequence = previous_last_message_sequence + 1
        new_message_sequences: list[int] = []
        for item in converged_items:
            materialized = self._context.checkpoints._materialize_canonical_message_projection(
                connection,
                item,
                thread_id=thread_id,
                session_id=session_id,
                checkpoint_ns=checkpoint_ns,
                commit_id=commit_id,
                message_sequence=next_message_sequence,
                timestamp=_now(),
            )
            if materialized is None:
                continue
            sequence, _message_id, _role = materialized
            if sequence > previous_last_message_sequence:
                new_message_sequences.append(sequence)
                next_message_sequence = sequence + 1

        if new_message_sequences:
            commit_update = connection.execute(
                "UPDATE storage_commits SET first_message_sequence = ?, "
                "last_message_sequence = ? WHERE commit_id = ?",
                (min(new_message_sequences), max(new_message_sequences), commit_id),
            )
            if commit_update.rowcount != 1:
                raise RuntimeError(
                    "internal terminal storage commit message sequence 更新失败"
                )
            meta_update = connection.execute(
                "UPDATE database_meta SET last_message_sequence = ?, "
                "history_view_revision = history_view_revision + 1, updated_at = ? "
                "WHERE singleton_id = 1",
                (max(new_message_sequences), _now()),
            )
            if meta_update.rowcount != 1:
                raise RuntimeError(
                    "internal terminal last_message_sequence 更新失败"
                )

        timestamp = _now()
        execution_update = connection.execute(
            "UPDATE executions SET outcome = ?, updated_at = ? "
            "WHERE session_id = ? AND thread_id = ? AND execution_id = ? "
            "AND execution_origin = 'internal' AND turn_id IS NULL",
            (outcome, timestamp, session_id, thread_id, execution_id),
        )
        if execution_update.rowcount != 1:
            raise RuntimeError("internal terminal execution outcome 更新失败")
        self._converge_execution_calls_and_assemblies(
            connection, session_id=session_id, thread_id=thread_id,
            execution_id=execution_id, latest_model_call_id=model_call_id,
            outcome=outcome, timestamp=timestamp, control_correction=control_correction,
        )
        self._storage._commit_connection(connection)
        return commit_id
