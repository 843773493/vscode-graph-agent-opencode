"""Rollout v2 Turn/execution/model-call 持久化 owner。

这些 mixin 只依赖 RolloutStorage 提供的 SQLite、JSONL transaction 和 domain
ports；它们不承担 LangChain/provider projection，也不读取 v1 数据。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    CommitKind,
    CommitMode,
    ControlOutcome,
    SemanticKind,
    TurnScope,
    TurnStatus,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.validation import validate_turn_transition
from app.services.infrastructure.rollout_context.execution.dispatch import (
    RolloutExecutionDispatchMixin,
)
from app.services.infrastructure.rollout_context.execution.recovery import (
    RolloutExecutionRecoveryMixin,
)
from app.services.infrastructure.rollout_context.execution.terminal_helpers import (
    RolloutTerminalProjectionMixin,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_idempotency_commit,
    read_committed_item_identities,
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


class RolloutExecutionsMixin(
    RolloutExecutionDispatchMixin,
    RolloutExecutionRecoveryMixin,
    RolloutTerminalProjectionMixin,
):
    """Execution attempt、resume/lost 与 terminal convergence owner。"""

    def converge_execution(
        self,
        thread_id: str,
        *,
        turn_id: str,
        execution_id: str,
        outcome: str,
        turn_status: str,
        items: Sequence[CanonicalItemRecord] = (),
        final_item_id: str | None = None,
        assembly_id: str | None = None,
        checkpoint_ns: str = "",
    ) -> int:
        """原子收敛 execution/model/assembly/Turn 与可选 output item。"""
        strict_text(thread_id, field="session_id")
        strict_text(turn_id, field="turn_id")
        strict_text(execution_id, field="execution_id")
        outcome = strict_text(outcome, field="execution outcome")
        turn_status = strict_text(turn_status, field="Turn.status")
        final_item_id = strict_optional_text(final_item_id, field="final_item_id")
        assembly_id = strict_optional_text(assembly_id, field="assembly_id")
        strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        if outcome not in {value.value for value in ControlOutcome}:
            raise ValueError(f"未知 execution outcome: {outcome}")
        if turn_status not in {value.value for value in TurnStatus}:
            raise ValueError(f"未知 Turn.status: {turn_status}")
        if turn_status == "completed" and not final_item_id:
            raise ValueError("completed Turn 必须指定 final_item_id")
        if turn_status == TurnStatus.COMPLETED_EMPTY.value and items:
            raise ValueError("completed_empty Turn 不得包含 canonical output item")
        if turn_status in {
            TurnStatus.OPEN.value,
            TurnStatus.ACTIVE.value,
        }:
            raise ValueError("terminal convergence 只能收敛终态 Turn")
        if turn_status != "completed" and final_item_id is not None:
            raise ValueError(
                f"只有 completed Turn 可以指定 final_item_id；当前 status={turn_status}"
            )
        allowed_outcomes = {
            TurnStatus.COMPLETED.value: {ControlOutcome.COMPLETED.value},
            TurnStatus.COMPLETED_EMPTY.value: {ControlOutcome.COMPLETED_EMPTY.value},
            TurnStatus.INTERRUPTED.value: {ControlOutcome.INTERRUPTED.value},
            TurnStatus.CANCELLED.value: {ControlOutcome.CANCELLED.value},
            TurnStatus.FAILED.value: {ControlOutcome.FAILED.value},
            TurnStatus.UNKNOWN.value: {
                ControlOutcome.UNKNOWN.value,
                ControlOutcome.EXECUTION_LOST.value,
            },
        }
        if outcome not in allowed_outcomes.get(turn_status, set()):
            raise ValueError(
                "Turn.status 与 terminal outcome 不匹配: "
                f"status={turn_status}, outcome={outcome}"
            )
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                turn = connection.execute(
                    "SELECT status, final_item_id FROM turn_records WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if turn is None:
                    raise KeyError(f"Turn 不存在: {turn_id}")
                execution = connection.execute(
                    "SELECT outcome FROM executions WHERE execution_id = ? AND turn_id = ?",
                    (execution_id, turn_id),
                ).fetchone()
                if execution is None:
                    raise KeyError(f"execution 不存在或不属于 Turn: {execution_id}")
                current_execution_outcome = strict_text(
                    execution[0], field="executions.outcome"
                )
                if current_execution_outcome not in {
                    ControlOutcome.UNKNOWN.value,
                    outcome,
                }:
                    raise ValueError(
                        "execution outcome 已收敛且与 terminal outcome 冲突: "
                        f"execution={execution_id}, existing={current_execution_outcome}, requested={outcome}"
                    )
                current_status = strict_text(
                    turn[0], field="turn_records.status"
                )
                existing_final_item_id = strict_optional_text(
                    turn[1], field="turn_records.final_item_id"
                )
                if current_status in {
                    "completed",
                    "completed_empty",
                    "failed",
                    "cancelled",
                }:
                    if (
                        current_status == turn_status
                        and existing_final_item_id == final_item_id
                    ):
                        idempotency_key = (
                            f"terminal:{turn_id}:{execution_id}:{outcome}"
                        )
                        expected_mode = (
                            CommitMode.ITEM_BEARING.value
                            if items
                            else CommitMode.METADATA_ONLY.value
                        )
                        committed = load_committed_idempotency_commit(
                            connection,
                            commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                            subject_id=turn_id,
                            idempotency_key=idempotency_key,
                            commit_mode=expected_mode,
                            outcome=outcome,
                            metadata={},
                            item_count=len(items),
                        )
                        if committed is None:
                            raise RuntimeError(
                                "Turn 已终态但 terminal convergence commit 缺失"
                            )
                        committed_items = tuple(
                            (item_id, content_hash)
                            for item_id, content_hash, _sequence in (
                                read_committed_item_identities(
                                    connection,
                                    commit_id=committed.commit_id,
                                )
                            )
                        )
                        requested_items = tuple(
                            (item.item_id, item.content_hash) for item in items
                        )
                        if committed_items != requested_items:
                            raise ValueError(
                                "terminal convergence 幂等键冲突：item identity/content 不一致"
                            )
                        return committed.commit_id
                    raise ValueError(f"Turn 已终态，禁止再次收敛: {turn_id}")
                try:
                    validate_turn_transition(current_status, turn_status)
                except ItemSchemaError as error:
                    raise ValueError(str(error)) from error
                if turn_status == "completed" and final_item_id:
                    known = connection.execute(
                        "SELECT status, semantic_kind, turn_id FROM item_catalog WHERE item_id = ?",
                        (final_item_id,),
                    ).fetchone()
                    candidate = next(
                        (item for item in items if item.item_id == final_item_id), None
                    )
                    if (
                        (known is None and candidate is None)
                        or (
                            known is not None
                            and (
                                strict_text(
                                    known[0], field="item_catalog.status"
                                )
                                != CanonicalItemStatus.COMPLETED.value
                                or strict_text(
                                    known[1], field="item_catalog.semantic_kind"
                                )
                                != SemanticKind.ASSISTANT_OUTPUT.value
                                or strict_text(known[2], field="item_catalog.turn_id")
                                != turn_id
                            )
                        )
                        or (
                            candidate is not None
                            and (
                                candidate.status != CanonicalItemStatus.COMPLETED.value
                                or candidate.semantic_kind
                                != SemanticKind.ASSISTANT_OUTPUT.value
                                or candidate.turn_id != turn_id
                            )
                        )
                    ):
                        raise ValueError(
                            "final_item_id 必须指向 completed canonical item"
                        )
                    existing_final_item = None
                    if known is not None and candidate is None:
                        existing_final_item = self._read_canonical_item_from_catalog(
                            connection,
                            thread_id=thread_id,
                            checkpoint_ns=checkpoint_ns,
                            item_id=final_item_id,
                        )
                else:
                    existing_final_item = None
                if assembly_id is not None:
                    assembly = connection.execute(
                        "SELECT session_id, turn_id, execution_id, status, outcome FROM context_assemblies WHERE assembly_id = ?",
                        (assembly_id,),
                    ).fetchone()
                    if assembly is None:
                        raise KeyError(f"sealed assembly 不存在: {assembly_id}")
                    assembly_session_id = strict_text(
                        assembly[0], field="context_assemblies.session_id"
                    )
                    assembly_turn_id = strict_text(
                        assembly[1], field="context_assemblies.turn_id"
                    )
                    assembly_execution_id = strict_text(
                        assembly[2], field="context_assemblies.execution_id"
                    )
                    assembly_status = strict_text(
                        assembly[3], field="context_assemblies.status"
                    )
                    if (
                        assembly_session_id != thread_id
                        or assembly_turn_id != turn_id
                        or assembly_execution_id != execution_id
                        or assembly_status not in {"sealed", "terminal"}
                    ):
                        raise ValueError("terminal convergence assembly 关联冲突")
                    assembly_outcome = strict_optional_text(
                        assembly[4], field="context_assemblies.outcome"
                    )
                    if assembly_outcome not in {None, outcome}:
                        raise ValueError("assembly outcome 与 terminal outcome 冲突")
                for item in items:
                    if (
                        item.turn_id != turn_id
                        or item.turn_scope != TurnScope.TURN_MEMBER
                    ):
                        raise ValueError(
                            "terminal convergence item 必须是当前 Turn 的 turn_member: "
                            f"item={item.item_id}"
                        )
                model_call_outcomes = connection.execute(
                    "SELECT model_call_id, outcome FROM model_calls "
                    "WHERE execution_id = ? ORDER BY attempt ASC",
                    (execution_id,),
                ).fetchall()
                latest_model_call_id: str | None = None
                latest_model_call_outcome: str | None = None
                if model_call_outcomes:
                    latest_model_call_id = strict_text(
                        model_call_outcomes[-1][0], field="model_calls.model_call_id"
                    )
                    latest_model_call_outcome = strict_text(
                        model_call_outcomes[-1][1], field="model_calls.outcome"
                    )
                    if latest_model_call_outcome not in {
                        ControlOutcome.UNKNOWN.value,
                        outcome,
                    }:
                        raise ValueError(
                            "terminal convergence 与最新 model_call outcome 冲突: "
                            f"model_call={latest_model_call_id}, "
                            f"existing={latest_model_call_outcome}, requested={outcome}"
                        )
                last_item_row = connection.execute(
                    "SELECT last_item_sequence FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if last_item_row is None:
                    raise RuntimeError("terminal convergence 缺少 last_item_sequence")
                last_item_sequence = strict_non_negative_int(
                    last_item_row[0], field="database_meta.last_item_sequence"
                )
                converged_items = tuple(
                    replace(
                        item,
                        item_sequence=last_item_sequence + index + 1,
                    )
                    for index, item in enumerate(items)
                )
                connection.execute("BEGIN IMMEDIATE")
                commit_id, _offset = self._append_v2_records_transaction(
                    connection,
                    thread_id,
                    checkpoint_ns,
                    converged_items,
                    commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                    outcome=outcome,
                    subject_id=turn_id,
                    idempotency_key=f"terminal:{turn_id}:{execution_id}:{outcome}",
                    begin_transaction=False,
                )
                # item-bearing terminal convergence 不能假定另一个 checkpoint
                # writer 已经先建立 messages。canonical item、message locator、
                # bounded message projection、Turn 坐标和 terminal pointer 必须
                # 在这一 SQLite 事务内同时可见；否则 output item 虽已 durable，
                # history 仍会把它当成缺失 final projection。
                projection_items = tuple(
                    item.item_id
                    for item in (
                        *converged_items,
                        *((existing_final_item,) if existing_final_item else ()),
                    )
                )
                item_by_id = {
                    item.item_id: item
                    for item in (
                        *converged_items,
                        *((existing_final_item,) if existing_final_item else ()),
                    )
                }
                if turn_status == TurnStatus.COMPLETED.value and final_item_id:
                    root_row = connection.execute(
                        "SELECT root_input_item_id FROM turn_records WHERE turn_id = ?",
                        (turn_id,),
                    ).fetchone()
                    if root_row is None or not root_row[0]:
                        raise RuntimeError(f"Turn 缺少 root_input_item_id: {turn_id}")
                    root_item_id = strict_text(
                        root_row[0], field="turn_records.root_input_item_id"
                    )
                    if root_item_id not in item_by_id:
                        item_by_id[root_item_id] = (
                            self._read_canonical_item_from_catalog(
                                connection,
                                thread_id=thread_id,
                                checkpoint_ns=checkpoint_ns,
                                item_id=root_item_id,
                            )
                        )
                        projection_items = (root_item_id, *projection_items)
                message_meta = connection.execute(
                    "SELECT last_message_sequence FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if message_meta is None:
                    raise RuntimeError("terminal convergence 缺少 message sequence 元数据")
                previous_last_message_sequence = strict_non_negative_int(
                    message_meta[0], field="database_meta.last_message_sequence"
                )
                next_message_sequence = previous_last_message_sequence + 1
                active_branch_row = connection.execute(
                    "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                    (checkpoint_ns,),
                ).fetchone()
                if active_branch_row is None or not active_branch_row[0]:
                    raise RuntimeError(
                        f"terminal convergence 缺少 active branch: {checkpoint_ns!r}"
                    )
                new_message_sequences: list[int] = []
                for item_id in projection_items:
                    item = item_by_id[item_id]
                    materialized = self._materialize_canonical_message_projection(
                        connection,
                        item,
                        thread_id=thread_id,
                        checkpoint_ns=checkpoint_ns,
                        commit_id=commit_id,
                        message_sequence=next_message_sequence,
                        timestamp=_now(),
                    )
                    if materialized is None:
                        continue
                    materialized_sequence, _message_id, role = materialized
                    if materialized_sequence > previous_last_message_sequence:
                        new_message_sequences.append(materialized_sequence)
                        next_message_sequence = materialized_sequence + 1
                        if item.turn_id is not None:
                            self._upsert_turn(
                                connection,
                                item.turn_id,
                                materialized_sequence,
                                _message_id,
                                role,
                                strict_text(
                                    active_branch_row[0],
                                    field="checkpoint_namespace_state.active_branch_id",
                                ),
                                _now(),
                            )
                if new_message_sequences:
                    commit_update = connection.execute(
                        "UPDATE storage_commits SET first_message_sequence = ?, "
                        "last_message_sequence = ? WHERE commit_id = ?",
                        (
                            min(new_message_sequences),
                            max(new_message_sequences),
                            commit_id,
                        ),
                    )
                    if commit_update.rowcount != 1:
                        raise RuntimeError(
                            "terminal convergence storage commit message sequence 更新失败"
                        )
                    meta_update = connection.execute(
                        "UPDATE database_meta SET last_message_sequence = ?, "
                        "history_view_revision = history_view_revision + 1, "
                        "updated_at = ? WHERE singleton_id = 1",
                        (max(new_message_sequences), _now()),
                    )
                    if meta_update.rowcount != 1:
                        raise RuntimeError(
                            "terminal convergence last_message_sequence 更新失败"
                        )
                self._append_context_view_items(
                    connection,
                    checkpoint_ns=checkpoint_ns,
                    item_ids=tuple(item.item_id for item in converged_items),
                )
                timestamp = _now()
                execution_update = connection.execute(
                    "UPDATE executions SET outcome = ? WHERE execution_id = ? AND turn_id = ?",
                    (outcome, execution_id, turn_id),
                )
                if execution_update.rowcount != 1:
                    raise RuntimeError(
                        "terminal convergence execution outcome 更新失败"
                    )
                if latest_model_call_id is not None:
                    # 同一 execution 的旧 retry attempt 是独立的已收敛事实；
                    # terminal convergence 只闭合最新 attempt，不能把此前
                    # validation_failed/provider error 重写成最终成功。
                    model_call_update = connection.execute(
                        "UPDATE model_calls SET outcome = ?, dispatch_state = CASE WHEN ? = 'completed' THEN 'completed' WHEN ? IN ('failed', 'cancelled', 'interrupted') THEN 'failed' ELSE 'unknown' END WHERE model_call_id = ? AND execution_id = ?",
                        (
                            outcome,
                            outcome,
                            outcome,
                            latest_model_call_id,
                            execution_id,
                        ),
                    )
                    if model_call_update.rowcount != 1:
                        raise RuntimeError(
                            "terminal convergence latest model-call outcome 更新失败"
                        )
                turn_update = connection.execute(
                    "UPDATE turn_records SET status = ?, final_item_id = ?, last_execution_id = ?, updated_at = ? WHERE turn_id = ?",
                    (turn_status, final_item_id, execution_id, timestamp, turn_id),
                )
                if turn_update.rowcount != 1:
                    raise RuntimeError("terminal convergence TurnRecord 更新失败")
                history_status = _V2_TO_HISTORY_STATUS.get(turn_status)
                if history_status is None:
                    raise ValueError(f"未知历史 Turn projection status: {turn_status}")
                if final_item_id is not None:
                    # `turns`/`context_view_turns` 只是历史读取所需的派生消息
                    # 坐标。terminal convergence 仍以 TurnRecord.final_item_id
                    # 为事实源，但必须在同一事务内把对应 message projection
                    # 的 final pointer 一并收敛；否则紧随其后的 checkpoint
                    # append 会把旧索引留在 running/NULL，history reader 只能
                    # 看到用户消息而无法证明最终 assistant item。
                    metadata_row = connection.execute(
                        "SELECT metadata_json FROM item_catalog WHERE item_id = ?",
                        (final_item_id,),
                    ).fetchone()
                    final_message_id: str | None = None
                    if metadata_row is not None:
                        metadata_value = strict_text(
                            metadata_row[0],
                            field="item_catalog.metadata_json",
                        )
                        try:
                            metadata = json.loads(metadata_value)
                        except json.JSONDecodeError as error:
                            raise RuntimeError(
                                "final item metadata_json 不是合法 JSON: "
                                f"{final_item_id}"
                            ) from error
                        if not isinstance(metadata, Mapping):
                            raise RuntimeError(
                                "final item metadata_json 必须是 object: "
                                f"{final_item_id}"
                            )
                        projection_message_id = metadata.get("projection_message_id")
                        if projection_message_id is not None:
                            final_message_id = strict_text(
                                projection_message_id,
                                field="projection_message_id",
                            )
                    if final_message_id is None and final_item_id.startswith("item-"):
                        final_message_id = final_item_id[len("item-") :]
                    final_message_row = (
                        connection.execute(
                            "SELECT message_sequence, message_id FROM messages "
                            "WHERE turn_id = ? AND message_id = ? "
                            "AND role = 'assistant' ORDER BY message_sequence DESC LIMIT 1",
                            (turn_id, final_message_id),
                        ).fetchone()
                        if final_message_id
                        else None
                    )
                    if final_message_row is None:
                        raise RuntimeError(
                            "terminal convergence 的 final_item_id 没有对应 assistant message projection: "
                            f"turn_id={turn_id}, final_item_id={final_item_id}"
                        )
                    final_message_sequence = strict_non_negative_int(
                        final_message_row[0], field="messages.message_sequence"
                    )
                    final_message_id = strict_text(
                        final_message_row[1], field="messages.message_id"
                    )
                    item_projection_update = connection.execute(
                        "UPDATE item_projections SET phase = 'final_answer', updated_at = ? WHERE item_id = ?",
                        (timestamp, final_item_id),
                    )
                    if item_projection_update.rowcount != 1:
                        raise RuntimeError(
                            "terminal convergence final item projection 更新失败"
                        )
                    message_projection_update = connection.execute(
                        "UPDATE message_projections SET phase = 'final_answer', updated_at = ? WHERE message_sequence = ?",
                        (timestamp, final_message_sequence),
                    )
                    if message_projection_update.rowcount != 1:
                        raise RuntimeError(
                            "terminal convergence final message projection 更新失败"
                        )
                    turn_projection_update = connection.execute(
                        "UPDATE turns SET final_message_sequence = ?, "
                        "final_message_id = ?, status = 'completed', updated_at = ? "
                        "WHERE turn_id = ?",
                        (
                            final_message_sequence,
                            final_message_id,
                            timestamp,
                            turn_id,
                        ),
                    )
                    if turn_projection_update.rowcount != 1:
                        raise RuntimeError(
                            "terminal convergence Turn message projection 更新失败"
                        )
                    connection.execute(
                        "UPDATE context_view_turns SET final_message_sequence = ? "
                        "WHERE turn_id = ?",
                        (final_message_sequence, turn_id),
                    )
                    self._ensure_active_view_turn(
                        connection,
                        checkpoint_ns=checkpoint_ns,
                        turn_id=turn_id,
                    )
                else:
                    # completed_empty、failed、cancelled、interrupted 和
                    # unknown 都没有 final item，但历史投影仍必须在同一
                    # terminal convergence 事务里离开 active/running。
                    connection.execute(
                        "UPDATE turns SET status = ?, updated_at = ? WHERE turn_id = ?",
                        (history_status, timestamp, turn_id),
                    )
                if assembly_id is not None:
                    connection.execute(
                        "UPDATE context_assemblies SET status = 'terminal', outcome = ?, terminal_at = ? WHERE assembly_id = ? AND session_id = ? AND turn_id = ? AND execution_id = ? AND status = 'sealed'",
                        (
                            outcome,
                            timestamp,
                            assembly_id,
                            thread_id,
                            turn_id,
                            execution_id,
                        ),
                    )
                self._commit_connection(connection)
                return commit_id
