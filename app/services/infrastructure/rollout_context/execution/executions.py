"""Rollout v2 Turn/execution/model-call 持久化 owner。

这些 mixin 只依赖 RolloutContextStore 提供的 SQLite、JSONL transaction 和 domain
ports；它们不承担 LangChain/provider projection，也不读取 v1 数据。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from app.core.sqlite_state import utc_now_text as _now
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
from app.domain.itemized.records import (
    CanonicalItemRecord,
)
from app.domain.itemized.validation import validate_turn_transition
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_idempotency_commit,
    read_committed_item_identities,
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


class RolloutExecutionsMixin:
    """Execution attempt、resume/lost 与 terminal convergence owner。"""

    def converge_execution(
        self,
        session_id: str,
        *,
        thread_id: str,
        turn_id: str | None,
        execution_id: str,
        outcome: str,
        turn_status: str | None,
        items: Sequence[CanonicalItemRecord] = (),
        final_item_id: str | None = None,
        assembly_id: str | None = None,
        checkpoint_ns: str = "",
        loss_reason: str | None = None,
    ) -> int:
        """原子收敛 execution/model/assembly/Turn 与可选 output item。"""
        return self._converge_execution(
            session_id, thread_id=thread_id, turn_id=turn_id, execution_id=execution_id,
            outcome=outcome, turn_status=turn_status, items=items, final_item_id=final_item_id,
            assembly_id=assembly_id, checkpoint_ns=checkpoint_ns, loss_reason=loss_reason,
        )

    def materialize_legacy_turn(
        self,
        session_id: str,
        *,
        thread_id: str,
        turn_id: str,
        execution_id: str,
        source_session_id: str,
        source_thread_id: str,
        turn_status: str,
        finalization_evidence: str,
        items: Sequence[CanonicalItemRecord],
        final_item_id: str | None,
        checkpoint_ns: str,
    ) -> int:
        """仅在显式 importer 的私有 staging 安装已校验的历史终态事实。"""
        from app.services.infrastructure.rollout_context.migration.staging import (
            ImportStagingStorage,
        )

        if not isinstance(self._storage, ImportStagingStorage):
            raise TypeError("历史终态物化只允许 importer 私有 staging")
        source_session_id = strict_text(source_session_id, field="legacy.source_session_id")
        source_thread_id = strict_text(source_thread_id, field="legacy.source_thread_id")
        if finalization_evidence == "explicit_legacy_final_marker":
            valid = turn_status == "completed" and final_item_id is not None
        elif finalization_evidence == "explicit_legacy_control_marker":
            valid = turn_status in {"failed", "interrupted", "cancelled"} and final_item_id is None
        else:
            valid = finalization_evidence in {
                "ambiguous_legacy_finalization", "legacy_finalization_unavailable",
                "legacy_final_marker_not_completed_assistant",
            } and turn_status == "unknown" and final_item_id is None
        if not valid:
            raise ValueError("legacy 历史终态与已校验 source evidence 不一致")
        # 源 final 只能证明历史回答完成，不能伪造本地 Provider 成功。
        outcome = turn_status if finalization_evidence == "explicit_legacy_control_marker" else "unknown"
        return self._converge_execution(
            session_id, thread_id=thread_id, turn_id=turn_id, execution_id=execution_id,
            outcome=outcome, turn_status=turn_status, items=items, final_item_id=final_item_id,
            checkpoint_ns=checkpoint_ns,
            historical_source=(source_session_id, source_thread_id, finalization_evidence),
        )

    def _converge_execution(
        self,
        session_id: str,
        *,
        thread_id: str,
        turn_id: str | None,
        execution_id: str,
        outcome: str,
        turn_status: str | None,
        items: Sequence[CanonicalItemRecord] = (),
        final_item_id: str | None = None,
        assembly_id: str | None = None,
        checkpoint_ns: str = "",
        loss_reason: str | None = None,
        historical_source: tuple[str, str, str] | None = None,
    ) -> int:
        """运行终态与显式历史安装共用唯一 items/commit/projection 事务。"""
        session_id = strict_text(session_id, field="session_id")
        owner_thread_id = self._storage.fork_paths.thread_identity(
            session_id, strict_text(thread_id, field="thread_id")
        )
        turn_id = strict_optional_text(turn_id, field="turn_id")
        strict_text(execution_id, field="execution_id")
        outcome = strict_text(outcome, field="execution outcome")
        turn_status = strict_optional_text(turn_status, field="Turn.status")
        final_item_id = strict_optional_text(final_item_id, field="final_item_id")
        assembly_id = strict_optional_text(assembly_id, field="assembly_id")
        strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        loss_reason = strict_optional_text(loss_reason, field="execution lost reason")
        if loss_reason is not None and (
            turn_id is None or outcome != ControlOutcome.EXECUTION_LOST.value
        ):
            raise ValueError("loss_reason 只用于 user Turn 的 execution_lost 收口")
        terminal_key = (
            f"execution-lost:{turn_id}:{execution_id}" if loss_reason is not None
            else f"terminal:{turn_id}:{execution_id}:{outcome}"
        )
        terminal_metadata = (
            {"execution_id": execution_id, "reason": loss_reason}
            if loss_reason is not None else {}
        )
        if historical_source is not None:
            terminal_key = f"legacy-terminal:{turn_id}:{execution_id}"
            terminal_metadata = {
                "legacy_source_session_id": historical_source[0],
                "legacy_source_thread_id": historical_source[1],
                "finalization_evidence": historical_source[2],
            }
        if outcome not in {value.value for value in ControlOutcome}:
            raise ValueError(f"未知 execution outcome: {outcome}")
        if turn_id is None:
            if turn_status is not None or final_item_id is not None:
                raise ValueError(
                    "Turn-less internal convergence 不得指定 Turn.status 或 final_item_id"
                )
        else:
            if turn_status is None:
                raise ValueError("user_turn convergence 必须指定 Turn.status")
            if turn_status not in {value.value for value in TurnStatus}:
                raise ValueError(f"未知 Turn.status: {turn_status}")
            if turn_status == "completed" and not final_item_id:
                raise ValueError("completed Turn 必须指定 final_item_id")
            if turn_status == "completed" and not items:
                raise ValueError(
                    "completed terminal convergence 必须在同一提交中包含 final canonical item"
                )
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
            if historical_source is None and outcome not in allowed_outcomes.get(turn_status, set()):
                raise ValueError(
                    "Turn.status 与 terminal outcome 不匹配: "
                    f"status={turn_status}, outcome={outcome}"
                )
        with self._storage._lock(owner_thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(
                session_id,
                checkpoint_ns,
                thread_id=owner_thread_id,
            )
            with self._storage._connect(
                owner_thread_id,
                checkpoint_ns,
                session_id=session_id,
            ) as connection:
                self._storage._require_v2_runtime(connection)
                if turn_id is None:
                    connection.execute("BEGIN IMMEDIATE")
                    return self._converge_internal_execution(
                        connection,
                        session_id=session_id,
                        thread_id=owner_thread_id,
                        execution_id=execution_id,
                        outcome=outcome,
                        items=items,
                        assembly_id=assembly_id,
                        checkpoint_ns=checkpoint_ns,
                    )
                turn = connection.execute(
                    "SELECT status, final_item_id FROM turn_records "
                    "WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
                    (session_id, owner_thread_id, turn_id),
                ).fetchone()
                if turn is None:
                    raise KeyError(f"Turn 不存在: {turn_id}")
                execution = connection.execute(
                    "SELECT outcome, execution_origin FROM executions "
                    "WHERE session_id = ? AND thread_id = ? "
                    "AND execution_id = ? AND turn_id = ?",
                    (session_id, owner_thread_id, execution_id, turn_id),
                ).fetchone()
                if execution is None:
                    raise KeyError(f"execution 不存在或不属于 Turn: {execution_id}")
                if strict_text(
                    execution[1], field="executions.execution_origin"
                ) != "user_turn":
                    raise ValueError(
                        "terminal Turn convergence 只接受 user_turn execution: "
                        f"execution_id={execution_id}"
                    )
                current_execution_outcome = strict_text(
                    execution[0], field="executions.outcome"
                )
                control_correction = (
                    current_execution_outcome == ControlOutcome.EXECUTION_LOST.value
                    and outcome in {ControlOutcome.CANCELLED.value, ControlOutcome.INTERRUPTED.value}
                )
                if current_execution_outcome not in {
                    ControlOutcome.UNKNOWN.value,
                    outcome,
                } and not control_correction:
                    raise ValueError(
                        "execution outcome 已收敛且与 terminal outcome 冲突: "
                        f"execution={execution_id}, existing={current_execution_outcome}, requested={outcome}"
                    )
                current_status = strict_text(
                    turn[0], field="turn_records.status"
                )
                if historical_source is not None:
                    historical = connection.execute(
                        "SELECT acceptance.identity_origin,turn.initial_execution_id,turn.last_execution_id,"
                        "execution.first_model_call_id,execution.last_model_call_id,turn.root_input_item_id "
                        "FROM turn_records turn JOIN turn_acceptances acceptance ON "
                        "acceptance.session_id=turn.session_id AND acceptance.thread_id=turn.thread_id "
                        "AND acceptance.turn_id=turn.turn_id JOIN executions execution ON "
                        "execution.session_id=turn.session_id AND execution.thread_id=turn.thread_id "
                        "AND execution.execution_id=turn.initial_execution_id "
                        "WHERE turn.session_id=? AND turn.thread_id=? AND turn.turn_id=?",
                        (session_id, owner_thread_id, turn_id),
                    ).fetchone()
                    if historical is None or historical[:5] != (
                        "legacy_synthetic", execution_id, execution_id, None, None,
                    ):
                        raise ValueError("历史终态物化缺少 source-derived synthetic acceptance/execution")
                    root = self._storage._read_canonical_item_from_catalog(
                        connection, session_id=session_id, thread_id=owner_thread_id,
                        checkpoint_ns=checkpoint_ns, item_id=historical[5],
                    )
                    annotations = root.extensions.get("boxteam.acceptance.annotations")
                    if not isinstance(annotations, Mapping):
                        raise ValueError("历史终态物化缺少 acceptance source annotations")
                    lineage = annotations.get("value")
                    if not isinstance(lineage, Mapping) or (
                        lineage.get("legacy_source_session_id"),
                        lineage.get("legacy_source_thread_id"),
                    ) != historical_source[:2]:
                        raise ValueError("历史终态物化的 acceptance source lineage 不一致")
                    provider_fact = connection.execute(
                        "SELECT EXISTS(SELECT 1 FROM model_calls WHERE session_id=? "
                        "AND thread_id=? AND execution_id=?) OR EXISTS(SELECT 1 "
                        "FROM context_assemblies WHERE session_id=? AND thread_id=? "
                        "AND execution_id=?)",
                        (session_id, owner_thread_id, execution_id,
                         session_id, owner_thread_id, execution_id),
                    ).fetchone()[0]
                    if provider_fact:
                        raise ValueError("历史终态物化不得包含 Provider ModelCall/assembly 事实")
                existing_final_item_id = strict_optional_text(
                    turn[1], field="turn_records.final_item_id"
                )
                if current_status in {
                    "completed",
                    "completed_empty",
                    "interrupted",
                    "failed",
                    "cancelled",
                    "unknown",
                } and not (
                    current_status == TurnStatus.UNKNOWN.value
                    and outcome in {ControlOutcome.CANCELLED.value, ControlOutcome.INTERRUPTED.value}
                ):
                    if (
                        current_status == turn_status
                        and existing_final_item_id == final_item_id
                    ):
                        expected_mode = (
                            CommitMode.ITEM_BEARING.value
                            if items
                            else CommitMode.METADATA_ONLY.value
                        )
                        committed = load_committed_idempotency_commit(
                            connection,
                            commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                            subject_id=turn_id,
                            idempotency_key=terminal_key,
                            commit_mode=expected_mode,
                            outcome=outcome,
                            metadata=terminal_metadata,
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
                if historical_source is None:
                    try:
                        validate_turn_transition(current_status, turn_status)
                    except ItemSchemaError as error:
                        raise ValueError(str(error)) from error
                elif current_status != TurnStatus.OPEN.value:
                    raise ValueError("历史终态物化只允许未发布的 synthetic open Turn")
                if turn_status == "completed" and final_item_id:
                    known = connection.execute(
                        "SELECT status, semantic_kind, turn_id, execution_id "
                        "FROM item_catalog WHERE item_id = ?",
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
                                or strict_text(
                                    known[3], field="item_catalog.execution_id"
                                )
                                != execution_id
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
                    if known is not None and candidate is None:
                        raise ValueError(
                            "completed terminal convergence 的 final item 必须属于同一 item-bearing commit"
                        )
                if assembly_id is not None:
                    assembly = connection.execute(
                        "SELECT session_id, thread_id, turn_id, execution_id, "
                        "status, outcome FROM context_assemblies WHERE assembly_id = ?",
                        (assembly_id,),
                    ).fetchone()
                    if assembly is None:
                        raise KeyError(f"sealed assembly 不存在: {assembly_id}")
                    assembly_session_id = strict_text(
                        assembly[0], field="context_assemblies.session_id"
                    )
                    assembly_turn_id = strict_text(
                        assembly[2], field="context_assemblies.turn_id"
                    )
                    assembly_execution_id = strict_text(
                        assembly[3], field="context_assemblies.execution_id"
                    )
                    assembly_status = strict_text(
                        assembly[4], field="context_assemblies.status"
                    )
                    assembly_thread_id = strict_text(
                        assembly[1], field="context_assemblies.thread_id"
                    )
                    if (
                        assembly_session_id != session_id
                        or assembly_thread_id != owner_thread_id
                        or assembly_turn_id != turn_id
                        or assembly_execution_id != execution_id
                        or assembly_status != "sealed"
                    ):
                        raise ValueError("terminal convergence assembly 关联冲突")
                    assembly_outcome = strict_optional_text(
                        assembly[5], field="context_assemblies.outcome"
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
                latest_model_call_id = self._terminal_model_call_id(
                    connection, session_id=session_id, thread_id=owner_thread_id,
                    execution_id=execution_id, outcome=outcome,
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
                commit_id, _offset = self._storage._append_v2_records_transaction(
                    connection,
                    owner_thread_id,
                    checkpoint_ns,
                    converged_items,
                    session_id=session_id,
                    commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                    outcome=outcome,
                    subject_id=turn_id,
                    idempotency_key=terminal_key,
                    metadata=terminal_metadata,
                    begin_transaction=False,
                )
                for item in converged_items:
                    self._storage._bind_canonical_item_execution(
                        connection,
                        item_id=item.item_id,
                        execution_id=execution_id,
                    )
                # item-bearing terminal convergence 不能假定另一个 checkpoint
                # writer 已经先建立 messages。canonical item、message locator、
                # bounded message projection、Turn 坐标和 terminal pointer 必须
                # 在这一 SQLite 事务内同时可见；否则 output item 虽已 durable，
                # history 仍会把它当成缺失 final projection。
                projection_items = tuple(
                    item.item_id for item in converged_items
                )
                item_by_id = {
                    item.item_id: item for item in converged_items
                }
                if turn_status == TurnStatus.COMPLETED.value and final_item_id:
                    root_row = connection.execute(
                        "SELECT root_input_item_id FROM turn_records "
                        "WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
                        (session_id, owner_thread_id, turn_id),
                    ).fetchone()
                    if root_row is None or not root_row[0]:
                        raise RuntimeError(f"Turn 缺少 root_input_item_id: {turn_id}")
                    root_item_id = strict_text(
                        root_row[0], field="turn_records.root_input_item_id"
                    )
                    if root_item_id not in item_by_id:
                        item_by_id[root_item_id] = (
                            self._storage._read_canonical_item_from_catalog(
                                connection,
                                session_id=session_id,
                                thread_id=owner_thread_id,
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
                    materialized = self._context.checkpoints._materialize_canonical_message_projection(
                        connection,
                        item,
                        thread_id=owner_thread_id,
                        session_id=session_id,
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
                            self._storage._upsert_turn(
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
                self._storage._append_context_view_items(
                    connection,
                    checkpoint_ns=checkpoint_ns,
                    item_ids=tuple(item.item_id for item in converged_items),
                    require_view=True,
                )
                timestamp = _now()
                execution_update = connection.execute(
                    "UPDATE executions SET outcome = ?, updated_at = ? "
                    "WHERE session_id = ? AND thread_id = ? "
                    "AND execution_id = ? AND turn_id = ?",
                    (
                        outcome,
                        timestamp,
                        session_id,
                        owner_thread_id,
                        execution_id,
                        turn_id,
                    ),
                )
                if execution_update.rowcount != 1:
                    raise RuntimeError(
                        "terminal convergence execution outcome 更新失败"
                    )
                turn_update = connection.execute(
                    "UPDATE turn_records SET status = ?, final_item_id = ?, "
                    "last_execution_id = ?, updated_at = ? "
                    "WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
                    (
                        turn_status,
                        final_item_id,
                        execution_id,
                        timestamp,
                        session_id,
                        owner_thread_id,
                        turn_id,
                    ),
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
                    final_message_rows = connection.execute(
                        "SELECT message.message_sequence,message.message_id FROM item_catalog item "
                        "JOIN messages message ON message.jsonl_offset=item.jsonl_offset "
                        "AND message.jsonl_length=item.jsonl_length "
                        "WHERE item.item_id=? AND message.turn_id=? AND message.role='assistant'",
                        (final_item_id, turn_id),
                    ).fetchall()
                    if len(final_message_rows) != 1:
                        raise RuntimeError(
                            "terminal convergence 的 final_item_id 没有对应 assistant message projection: "
                            f"turn_id={turn_id}, final_item_id={final_item_id}"
                        )
                    final_message_row = final_message_rows[0]
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
                self._converge_execution_calls_and_assemblies(
                    connection, session_id=session_id, thread_id=owner_thread_id,
                    execution_id=execution_id, latest_model_call_id=latest_model_call_id,
                    outcome=outcome, timestamp=timestamp, control_correction=control_correction,
                )
                self._storage._commit_connection(connection)
                return commit_id
