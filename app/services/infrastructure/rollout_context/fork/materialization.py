"""v2 cross-session fork identity/materialization owners。

所有复制操作只消费已提交 v2 storage state；source 坐标进入 lineage audit，
目标运行时只使用 target-local identity。
"""

from __future__ import annotations

import re
from uuid import uuid4

from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import CommitKind, CommitMode, ControlOutcome
from app.services.infrastructure.rollout_context.fork.assembly_copy import (
    copy_selected_context_for_fork,
    prepare_selected_detail_mappings,
)
from app.services.infrastructure.rollout_context.fork.node_debug_journal import (
    NodeDebugForkJournalMixin,
)
from app.services.infrastructure.rollout_context.fork.overlay_copy import (
    ForkOverlayCopyMixin,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    non_negative_int,
    one_of_text,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.runtime.detail_fork import (
    ForkDetailCapability,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.storage.transaction import strict_text


def _optional_identity(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return required_text(value, field=field)


def _required_sha256(value: object, *, field: str) -> str:
    digest = required_text(value, field=field)
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError(f"{field} 必须是小写 SHA-256 hex digest")
    return digest


class ForkMaterializationMixin(NodeDebugForkJournalMixin, ForkOverlayCopyMixin):
    """fork journal、overlay copy 和 target materialization owner。"""

    def begin_fork_materialization(
        self,
        *,
        target_session_id: str,
        source_session_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_mode: str,
        relationship: str,
        checkpoint_ns: str,
        source_thread_id: str,
        target_thread_id: str,
        source_snapshot_sha256: str,
        fork_id: str | None = None,
        materialization_id: str | None = None,
    ) -> tuple[str, str]:
        """为目标 rollout 建立一次可恢复的 fork 物化 journal。"""
        target_session_id = required_text(target_session_id, field="target_session_id")
        source_session_id = required_text(source_session_id, field="source_session_id")
        if target_session_id == source_session_id:
            raise ValueError("fork target_session_id 不能与 source_session_id 相同")
        source_checkpoint_id = optional_text(
            source_checkpoint_id, field="source_checkpoint_id"
        )
        source_view_id = optional_text(source_view_id, field="source_view_id")
        fork_mode = one_of_text(
            fork_mode,
            {"context_fork", "history_prefix_fork", "full_rollout_copy"},
            field="fork_mode",
        )
        relationship = one_of_text(
            relationship, {"detached", "pinned"}, field="relationship"
        )
        if not isinstance(checkpoint_ns, str):
            raise TypeError("fork checkpoint_ns 必须是字符串")
        source_thread_id = required_text(source_thread_id, field="source_thread_id")
        target_thread_id = required_text(target_thread_id, field="target_thread_id")
        source_snapshot_sha256 = _required_sha256(
            source_snapshot_sha256, field="source_snapshot_sha256"
        )
        self._resolve_thread_node(source_session_id, source_thread_id)
        if self.main_thread_id(target_session_id) != target_thread_id:
            raise RuntimeError("fork journal target thread 与 target main owner 不匹配")
        self.initialize(
            target_session_id,
            checkpoint_ns,
            thread_id=target_thread_id,
        )
        materialization_id = _optional_identity(
            materialization_id, field="materialization_id"
        ) or uuid4().hex
        fork_id = _optional_identity(fork_id, field="fork_id") or uuid4().hex
        with (
            self._lock(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ),
            self._connect(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ) as connection,
        ):
            self._require_v2_runtime(connection)
            active = connection.execute(
                "SELECT materialization_id FROM fork_materializations WHERE status IN ('prepared', 'target_committed') LIMIT 1"
            ).fetchone()
            if active is not None:
                active_id = required_text(
                    active[0], field="fork_materializations.materialization_id"
                )
                raise RuntimeError(
                    f"目标 rollout 已存在未完成的 fork 物化: {active_id}"
                )
            meta = connection.execute(
                "SELECT committed_jsonl_offset FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if meta is None:
                raise RuntimeError(
                    "database_meta singleton 缺失，不能建立 fork journal"
                )
            rollback_offset = non_negative_int(
                meta[0], field="database_meta.committed_jsonl_offset"
            )
            result = connection.execute(
                "INSERT INTO fork_materializations(materialization_id, fork_id, target_session_id, source_session_id, source_checkpoint_id, source_view_id, fork_mode, relationship, status, rollback_jsonl_offset, created_at, source_thread_id, target_thread_id, checkpoint_ns, source_snapshot_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?)",
                (
                    materialization_id,
                    fork_id,
                    target_session_id,
                    source_session_id,
                    source_checkpoint_id,
                    source_view_id,
                    fork_mode,
                    relationship,
                    rollback_offset,
                    _now(),
                    source_thread_id,
                    target_thread_id,
                    checkpoint_ns,
                    source_snapshot_sha256,
                ),
            )
            if result.rowcount != 1:
                raise RuntimeError("fork materialization journal 未写入")
            if fork_mode != "full_rollout_copy":
                prepare_selected_detail_mappings(
                    self, connection, source_session_id=source_session_id,
                    source_thread_id=source_thread_id, target_session_id=target_session_id,
                    target_thread_id=target_thread_id, source_checkpoint_id=source_checkpoint_id,
                    fork_id=fork_id, checkpoint_ns=checkpoint_ns, timestamp=_now(),
                )
            self._active_fork_materializations.add(
                (target_session_id, target_thread_id, checkpoint_ns)
            )
        return materialization_id, fork_id

    def materialize_fork_checkpoint(
        self, *, materialization_id: str, target_session_id: str,
        target_thread_id: str, checkpoint_ns: str, checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        current_messages: list[object],
    ) -> None:
        """prepared journal 的 checkpoint capability；仅引用已有 target item。"""
        with (self._lock(target_thread_id, checkpoint_ns, session_id=target_session_id),
              self._connect(target_thread_id, checkpoint_ns, session_id=target_session_id,
                            read_only=True) as connection):
            row = connection.execute(
                "SELECT target_session_id, target_thread_id, checkpoint_ns, status "
                "FROM fork_materializations WHERE materialization_id = ?",
                (materialization_id,),
            ).fetchone()
            if row != (target_session_id, target_thread_id, checkpoint_ns, "prepared"):
                raise RuntimeError("fork checkpoint capability 与 prepared journal scope 不一致")
            for index, message in enumerate(current_messages):
                message_id = self._codec().message_id(message, index)
                item = connection.execute(
                    "SELECT execution_id FROM item_catalog WHERE item_id = ?",
                    (f"item-{message_id}",),
                ).fetchone()
                if item is None or not isinstance(item[0], str) or not item[0]:
                    raise RuntimeError("fork checkpoint 只能引用已映射 execution 的 target canonical item")
        self.append_checkpoint(
            thread_id=target_thread_id, checkpoint_ns=checkpoint_ns, checkpoint=checkpoint,
            metadata=metadata, parent_checkpoint_id=None, current_messages=current_messages,
        )

    def abort_fork_materialization(
        self,
        materialization_id: str,
        *,
        target_session_id: str,
        checkpoint_ns: str = "",
    ) -> None:
        """显式回滚尚未提交的 fork；崩溃时由 initialize 执行同一恢复路径。"""
        materialization_id = required_text(
            materialization_id, field="materialization_id"
        )
        target_session_id = required_text(target_session_id, field="target_session_id")
        if not isinstance(checkpoint_ns, str):
            raise TypeError("fork checkpoint_ns 必须是字符串")
        target_thread_id = self.main_thread_id(target_session_id)
        with (
            self._lock(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ),
            self._connect(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ) as connection,
        ):
            self._require_v2_runtime(connection)
            row = connection.execute(
                "SELECT status FROM fork_materializations WHERE materialization_id = ? AND target_session_id = ?",
                (materialization_id, target_session_id),
            ).fetchone()
            if row is None:
                return
            status = one_of_text(
                row[0],
                {"prepared", "target_committed", "committed", "aborted"},
                field="fork_materializations.status",
            )
            if status in {"committed", "aborted"}:
                return
            self._recover_fork_materialization(
                target_session_id,
                target_thread_id,
                checkpoint_ns,
                connection,
                self.jsonl_path(
                    target_session_id,
                    checkpoint_ns,
                    thread_id=target_thread_id,
                ),
            )
        self._active_fork_materializations.discard(
            (target_session_id, target_thread_id, checkpoint_ns)
        )

    def commit_fork_materialization(
        self,
        materialization_id: str,
        *,
        target_session_id: str,
        source_session_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_mode: str,
        relationship: str,
        checkpoint_ns: str,
        source_thread_id: str,
        target_thread_id: str,
        source_snapshot_sha256: str,
        defer_completion: bool = False,
        detail_capability: ForkDetailCapability | None = None,
        detail_store: ContextPlanDetailStore | None = None,
    ) -> str:
        """在目标库收敛 fork，并幂等完成父库 retention。

        canonical 与 Checkpoint 通过 prepared journal 的专用物化写入。
        对外可见的边界在本方法的目标事务中：Turn
        finalization、运行态终止、唯一 provenance 和 ``target_committed`` 一起
        提交；父库 retention 完成后才进入 ``committed``。
        """
        materialization_id = required_text(
            materialization_id, field="materialization_id"
        )
        target_session_id = required_text(target_session_id, field="target_session_id")
        source_session_id = required_text(source_session_id, field="source_session_id")
        if target_session_id == source_session_id:
            raise ValueError("fork target_session_id 不能与 source_session_id 相同")
        source_checkpoint_id = optional_text(
            source_checkpoint_id, field="source_checkpoint_id"
        )
        source_view_id = optional_text(source_view_id, field="source_view_id")
        fork_mode = one_of_text(
            fork_mode,
            {"context_fork", "history_prefix_fork", "full_rollout_copy"},
            field="fork_mode",
        )
        relationship = one_of_text(
            relationship, {"detached", "pinned"}, field="relationship"
        )
        if not isinstance(checkpoint_ns, str):
            raise TypeError("fork checkpoint_ns 必须是字符串")
        requested_source_view_id = source_view_id
        source_thread_id = required_text(source_thread_id, field="source_thread_id")
        target_thread_id = required_text(target_thread_id, field="target_thread_id")
        source_snapshot_sha256 = _required_sha256(
            source_snapshot_sha256, field="source_snapshot_sha256"
        )
        self._resolve_thread_node(source_session_id, source_thread_id)
        if self.main_thread_id(target_session_id) != target_thread_id:
            raise RuntimeError("fork commit target thread 与 target main owner 不匹配")
        if source_view_id is None:
            source_view_id = self._fork_source_view_id(
                source_session_id,
                source_checkpoint_id,
                checkpoint_ns,
                source_thread_id=source_thread_id,
            )
        source_finalizations = (
            ()
            if fork_mode == "full_rollout_copy"
            else self._fork_completed_turn_finalizations(
                source_session_id,
                source_checkpoint_id,
                checkpoint_ns,
                source_thread_id=source_thread_id,
            )
        )
        with (
            self._lock(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ),
            self._connect(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ) as connection,
        ):
            self._require_v2_runtime(connection)
            journal = connection.execute(
                "SELECT fork_id, target_session_id, source_session_id, source_checkpoint_id, source_view_id, fork_mode, relationship, status, source_thread_id, target_thread_id, checkpoint_ns, source_snapshot_sha256 FROM fork_materializations WHERE materialization_id = ? AND target_session_id = ?",
                (materialization_id, target_session_id),
            ).fetchone()
            if journal is None:
                raise KeyError(f"fork materialization 不存在: {materialization_id}")
            fork_id = required_text(journal[0], field="fork_materializations.fork_id")
            journal_target_session_id = required_text(
                journal[1], field="fork_materializations.target_session_id"
            )
            journal_source_session_id = required_text(
                journal[2], field="fork_materializations.source_session_id"
            )
            journal_source_checkpoint_id = optional_text(
                journal[3], field="fork_materializations.source_checkpoint_id"
            )
            journal_source_view_id = optional_text(
                journal[4], field="fork_materializations.source_view_id"
            )
            journal_fork_mode = one_of_text(
                journal[5],
                {"context_fork", "history_prefix_fork", "full_rollout_copy"},
                field="fork_materializations.fork_mode",
            )
            journal_relationship = one_of_text(
                journal[6],
                {"detached", "pinned"},
                field="fork_materializations.relationship",
            )
            status = one_of_text(
                journal[7],
                {"prepared", "target_committed", "committed", "aborted"},
                field="fork_materializations.status",
            )
            journal_source_thread_id = required_text(
                journal[8], field="fork_materializations.source_thread_id"
            )
            journal_target_thread_id = required_text(
                journal[9], field="fork_materializations.target_thread_id"
            )
            journal_checkpoint_ns = strict_text(
                journal[10],
                field="fork_materializations.checkpoint_ns",
                allow_empty=True,
            )
            journal_snapshot_sha256 = _required_sha256(
                journal[11], field="fork_materializations.source_snapshot_sha256"
            )
            if journal_target_session_id != target_session_id:
                raise RuntimeError("fork journal target session identity 不一致")
            if journal_source_session_id != source_session_id:
                raise RuntimeError("fork journal source session identity 不一致")
            if journal_source_checkpoint_id != source_checkpoint_id:
                raise RuntimeError("fork journal source checkpoint identity 不一致")
            if journal_source_view_id != requested_source_view_id:
                raise RuntimeError("fork journal source view identity 不一致")
            if journal_fork_mode != fork_mode:
                raise RuntimeError("fork journal mode 与提交请求不一致")
            if journal_relationship != relationship:
                raise RuntimeError("fork journal relationship 与提交请求不一致")
            if journal_source_thread_id != source_thread_id:
                raise RuntimeError("fork journal source thread identity 不一致")
            if journal_target_thread_id != target_thread_id:
                raise RuntimeError("fork journal target thread identity 不一致")
            if journal_checkpoint_ns != checkpoint_ns:
                raise RuntimeError("fork journal checkpoint namespace 不一致")
            if journal_snapshot_sha256 != source_snapshot_sha256:
                raise RuntimeError("fork journal source snapshot hash 不一致")
            if status == "committed":
                self._active_fork_materializations.discard(
                    (target_session_id, target_thread_id, checkpoint_ns)
                )
                return fork_id
            if status == "target_committed":
                pass
            elif status != "prepared":
                raise RuntimeError(
                    f"fork materialization 状态不可提交: {materialization_id}={status}"
                )
            else:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_fork_debug_snapshot_ready(
                    connection,
                    materialization_id,
                    allow_ready=defer_completion
                    and fork_mode == "full_rollout_copy",
                )
                timestamp = _now()
                transaction_id = f"fork:{materialization_id}"
                active_branch_row = connection.execute(
                    "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                    (checkpoint_ns,),
                ).fetchone()
                active_branch_id = (
                    optional_text(
                        active_branch_row[0],
                        field="checkpoint_namespace_state.active_branch_id",
                    )
                    if active_branch_row is not None
                    else None
                )
                if fork_mode == "full_rollout_copy":
                    prepared_context_details = ()
                    self._map_copied_acceptance_identities(
                        connection,
                        source_session_id=source_session_id,
                        target_session_id=target_session_id,
                        fork_id=fork_id,
                        checkpoint_ns=checkpoint_ns,
                        timestamp=timestamp,
                    )
                    self._remap_full_copy_v2_entities(
                        connection,
                        source_session_id=source_session_id,
                        target_session_id=target_session_id,
                        fork_id=fork_id,
                        checkpoint_ns=checkpoint_ns,
                        timestamp=timestamp,
                    )
                else:
                    self._copy_source_overlays_for_fork(
                        connection,
                        source_session_id=source_session_id,
                        source_thread_id=source_thread_id,
                        target_session_id=target_session_id,
                        target_thread_id=target_thread_id,
                        fork_id=fork_id,
                        checkpoint_ns=checkpoint_ns,
                        timestamp=timestamp,
                    )
                    prepared_context_details = copy_selected_context_for_fork(
                        self, connection, source_session_id=source_session_id,
                        source_thread_id=source_thread_id, target_session_id=target_session_id,
                        target_thread_id=target_thread_id, source_checkpoint_id=source_checkpoint_id,
                        fork_id=fork_id, checkpoint_ns=checkpoint_ns, timestamp=timestamp,
                        detail_capability=detail_capability,
                    )
                    if prepared_context_details and detail_store is None:
                        raise ValueError("detail-unavailable: fork source 缺少目标 detail 发布 owner")
                if fork_mode != "full_rollout_copy":
                    for source_turn_id, source_final_message_id in source_finalizations:
                        identities = dict(connection.execute(
                            "SELECT entity_type, target_local_id FROM fork_identity_mappings "
                            "WHERE fork_id = ? AND ((entity_type = 'turn' AND source_local_id = ?) "
                            "OR (entity_type = 'message' AND source_local_id = ?))",
                            (fork_id, source_turn_id, source_final_message_id),
                        ).fetchall())
                        if not identities:
                            continue
                        if set(identities) != {"turn", "message"}:
                            raise RuntimeError("fork completed Turn 的 target identity mapping 不完整")
                        turn_id = identities["turn"]
                        final_message_id = identities["message"]
                        target_message = connection.execute(
                            "SELECT message_sequence, turn_id FROM messages WHERE message_id = ?",
                            (final_message_id,),
                        ).fetchone()
                        if target_message is None:
                            raise RuntimeError(
                                "fork completed Turn 的 final message 未物化到 target: "
                                f"turn_id={turn_id}, message_id={final_message_id}"
                            )
                        target_message_turn_id = required_text(
                            target_message[1], field="messages.turn_id"
                        )
                        if target_message_turn_id != turn_id:
                            raise RuntimeError(
                                "fork final message 的 Turn identity 不一致: "
                                f"turn_id={turn_id}, message_id={final_message_id}"
                            )
                        target_turn = connection.execute(
                            "SELECT 1 FROM turns WHERE turn_id = ?",
                            (turn_id,),
                        ).fetchone()
                        if target_turn is None:
                            raise RuntimeError(
                                f"fork completed Turn 未物化到 target turns: {turn_id}"
                            )
                        target_sequence = non_negative_int(
                            target_message[0], field="messages.message_sequence"
                        )
                        if target_sequence == 0:
                            raise RuntimeError("messages.message_sequence 不能为 0")
                        result = connection.execute(
                            "UPDATE turns SET final_message_sequence = ?, final_message_id = ?, status = 'completed', updated_at = ? WHERE turn_id = ?",
                            (target_sequence, final_message_id, timestamp, turn_id),
                        )
                        if result.rowcount != 1:
                            raise RuntimeError(
                                f"fork finalization 未更新 target Turn: {turn_id}"
                            )
                        context_view_rows = connection.execute(
                            "SELECT view_id, final_message_sequence FROM context_view_turns WHERE turn_id = ?",
                            (turn_id,),
                        ).fetchall()
                        if not context_view_rows:
                            raise RuntimeError(
                                f"fork finalization 缺少 context view Turn: {turn_id}"
                            )
                        pending_view_updates = 0
                        for view_id, existing_final_sequence in context_view_rows:
                            required_text(view_id, field="context_view_turns.view_id")
                            # 这里允许已完成的 view 行重复收敛；SQLite 对
                            # “写入相同值”返回 rowcount=0，不能把幂等成功误判成
                            # 缺失索引。但任何已有且不同的终态都必须拒绝覆盖。
                            if existing_final_sequence is not None:
                                existing_sequence = non_negative_int(
                                    existing_final_sequence,
                                    field="context_view_turns.final_message_sequence",
                                )
                                if existing_sequence != target_sequence:
                                    raise RuntimeError(
                                        "fork finalization 覆盖了不一致的 view final sequence: "
                                        f"view_id={view_id}, turn_id={turn_id}"
                                    )
                            else:
                                pending_view_updates += 1
                        if pending_view_updates:
                            result = connection.execute(
                                "UPDATE context_view_turns SET final_message_sequence = ? WHERE turn_id = ? AND final_message_sequence IS NULL",
                                (target_sequence, turn_id),
                            )
                            if result.rowcount != pending_view_updates:
                                raise RuntimeError(
                                    f"fork finalization 未完整更新 context view Turn: {turn_id}"
                                )
                        result = connection.execute(
                            "UPDATE message_projections SET phase = 'final_answer', updated_at = ? WHERE message_sequence = ?",
                            (timestamp, target_sequence),
                        )
                        if result.rowcount != 1:
                            raise RuntimeError(
                                "fork finalization 缺少 message projection: "
                                f"message_sequence={target_sequence}"
                            )
                        self._insert_control(
                            connection,
                            "checkpoint_finalized",
                            "turn",
                            turn_id,
                            active_branch_id,
                            None,
                            None,
                            {
                                "final_message_sequence": target_sequence,
                                "copied_from_session_id": source_session_id,
                                "copied_from_message_id": final_message_id,
                            },
                            transaction_id,
                            timestamp,
                        )

                active_statuses = (
                    "accepted",
                    "queued",
                    "running",
                    "streaming",
                    "waiting_input",
                    "paused",
                    "interrupt_pending",
                    "cancelling",
                )
                placeholders = ", ".join("?" for _ in active_statuses)
                unfinished = connection.execute(
                    f"SELECT turn_id FROM turns WHERE status IN ({placeholders}) AND final_message_sequence IS NULL ORDER BY turn_ordinal",
                    active_statuses,
                ).fetchall()
                for unfinished_row in unfinished:
                    turn_id = required_text(unfinished_row[0], field="turns.turn_id")
                    result = connection.execute(
                        "UPDATE turns SET status = 'cancelled', updated_at = ? WHERE turn_id = ?",
                        (timestamp, turn_id),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError(f"fork runtime Turn 未收敛: {turn_id}")
                    self._insert_control(
                        connection,
                        "turn_status",
                        "turn",
                        turn_id,
                        active_branch_id,
                        None,
                        None,
                        {
                            "status": "cancelled",
                            "reason": "fork_runtime_not_copied",
                        },
                        transaction_id,
                        timestamp,
                    )

                # v2 TurnRecord 不一定有兼容层 turns 行（provider block
                # 可以没有 LangChain message），因此必须独立收敛。full
                # copy 只复制历史数据，不复制 source runtime；所有未有
                # final_item_id 的运行态 Turn 在 target 中成为永久的
                # cancelled historical，不能被原 Turn resume/dispatch。
                v2_unfinished = connection.execute(
                    "SELECT turn_id FROM turn_records WHERE status IN ('open', 'active', 'interrupted', 'unknown') AND final_item_id IS NULL ORDER BY turn_ordinal"
                ).fetchall()
                for (raw_turn_id,) in v2_unfinished:
                    turn_id = required_text(raw_turn_id, field="turn_records.turn_id")
                    self._append_v2_records_transaction(
                        connection,
                        target_thread_id,
                        checkpoint_ns,
                        (),
                        session_id=target_session_id,
                        commit_kind=CommitKind.TERMINAL_CONVERGENCE.value,
                        commit_mode=CommitMode.METADATA_ONLY.value,
                        outcome=ControlOutcome.CANCELLED.value,
                        subject_id=turn_id,
                        idempotency_key=f"fork-cancelled:{turn_id}",
                        metadata={
                            "reason": "fork_source_runtime_not_copied",
                            "historical": True,
                        },
                        begin_transaction=False,
                    )
                    result = connection.execute(
                        "UPDATE turn_records SET status = 'cancelled', updated_at = ? WHERE turn_id = ?",
                        (timestamp, turn_id),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError(f"fork v2 runtime Turn 未收敛: {turn_id}")
                    execution_count = non_negative_int(
                        connection.execute(
                            "SELECT COUNT(*) FROM executions WHERE turn_id = ?",
                            (turn_id,),
                        ).fetchone()[0],
                        field="fork.turn.execution_count",
                    )
                    result = connection.execute(
                        "UPDATE executions SET outcome = 'cancelled', updated_at = ? WHERE turn_id = ?",
                        (timestamp, turn_id),
                    )
                    if result.rowcount != execution_count:
                        raise RuntimeError(
                            f"fork execution cancellation count 不一致: {turn_id}"
                        )
                    model_call_count = non_negative_int(
                        connection.execute(
                            "SELECT COUNT(*) FROM model_calls WHERE execution_id IN (SELECT execution_id FROM executions WHERE turn_id = ?)",
                            (turn_id,),
                        ).fetchone()[0],
                        field="fork.turn.model_call_count",
                    )
                    result = connection.execute(
                        "UPDATE model_calls SET outcome = 'cancelled', dispatch_state = 'failed' WHERE execution_id IN (SELECT execution_id FROM executions WHERE turn_id = ?)",
                        (turn_id,),
                    )
                    if result.rowcount != model_call_count:
                        raise RuntimeError(
                            f"fork model call cancellation count 不一致: {turn_id}"
                        )
                    sealed_assembly_count = non_negative_int(
                        connection.execute(
                            "SELECT COUNT(*) FROM context_assemblies WHERE turn_id = ? AND status = 'sealed'",
                            (turn_id,),
                        ).fetchone()[0],
                        field="fork.turn.sealed_assembly_count",
                    )
                    result = connection.execute(
                        "UPDATE context_assemblies SET status = 'terminal', outcome = 'cancelled', terminal_at = ? WHERE turn_id = ? AND status = 'sealed'",
                        (timestamp, turn_id),
                    )
                    if result.rowcount != sealed_assembly_count:
                        raise RuntimeError(
                            f"fork assembly cancellation count 不一致: {turn_id}"
                        )

                result = connection.execute(
                    "INSERT INTO fork_origins(fork_id, child_session_id, source_session_id, source_checkpoint_id, source_view_id, fork_mode, relationship, copied_message_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, (SELECT COUNT(*) FROM messages), ?)",
                    (
                        fork_id,
                        target_session_id,
                        source_session_id,
                        source_checkpoint_id,
                        source_view_id,
                        fork_mode,
                        relationship,
                        timestamp,
                    ),
                )
                if result.rowcount != 1:
                    raise RuntimeError("fork origin 未写入")
                control_sequence = self._insert_control(
                    connection,
                    "fork_created",
                    "fork",
                    fork_id,
                    active_branch_id,
                    None,
                    source_checkpoint_id,
                    {
                        "source_session_id": source_session_id,
                        "source_view_id": source_view_id,
                        "fork_mode": fork_mode,
                        "relationship": relationship,
                    },
                    transaction_id,
                    timestamp,
                )
                result = connection.execute(
                    "UPDATE database_meta SET last_control_sequence = ?, updated_at = ? WHERE singleton_id = 1",
                    (control_sequence, timestamp),
                )
                if result.rowcount != 1:
                    raise RuntimeError("fork control sequence 未写入 database_meta")
                result = connection.execute(
                    "UPDATE fork_materializations SET status = 'target_committed', copied_message_count = (SELECT COUNT(*) FROM messages), target_committed_at = ?, error_message = NULL WHERE materialization_id = ?",
                    (timestamp, materialization_id),
                )
                if result.rowcount != 1:
                    raise RuntimeError("fork materialization 未进入 target_committed")
                published = []
                try:
                    for prepared in prepared_context_details:
                        published.extend(detail_store.publish(
                            prepared, creation_record=self._prepared_target_record(target_session_id),
                        ))
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    committed = connection.execute(
                        "SELECT status FROM fork_materializations WHERE materialization_id=?",
                        (materialization_id,),
                    ).fetchone()
                    if committed != ("target_committed",) and published:
                        detail_store.discard_published(published)
                    raise

        # 私有 full-copy staging 先保留 target_committed journal；安装后才
        # 由正式 owner 补写 source retention 与 committed，不发布半成品。
        if defer_completion:
            return fork_id
        return self.complete_fork_materialization(
            materialization_id=materialization_id,
            target_session_id=target_session_id,
            target_thread_id=target_thread_id,
            checkpoint_ns=checkpoint_ns,
        )

    def complete_fork_materialization(
        self, *, materialization_id: str, target_session_id: str,
        target_thread_id: str, checkpoint_ns: str,
    ) -> str:
        """退出 source snapshot scope 后，仅由目标 journal 完成 retention。"""
        if self._fork_source_snapshot_for_session(target_session_id) is not None:
            raise RuntimeError("fork completion target 不能是 source snapshot")
        with self._connect(target_thread_id, checkpoint_ns,
                           session_id=target_session_id, read_only=True) as connection:
            row = connection.execute(
                "SELECT fork_id, source_session_id, source_checkpoint_id, source_view_id, "
                "relationship, status, target_session_id, target_thread_id, checkpoint_ns, "
                "source_snapshot_sha256 FROM fork_materializations WHERE materialization_id = ?",
                (materialization_id,),
            ).fetchone()
        if row is None or row[6:9] != (target_session_id, target_thread_id, checkpoint_ns):
            raise RuntimeError("fork completion journal target scope 不一致")
        fork_id = required_text(row[0], field="fork_materializations.fork_id")
        source_session_id = required_text(row[1], field="fork_materializations.source_session_id")
        if self._fork_source_snapshot_for_session(source_session_id) is not None:
            raise RuntimeError("fork retention 必须在 source snapshot scope 释放后完成")
        source_checkpoint_id = optional_text(row[2], field="fork_materializations.source_checkpoint_id")
        source_view_id = optional_text(row[3], field="fork_materializations.source_view_id")
        relationship = one_of_text(row[4], {"pinned", "detached"}, field="fork_materializations.relationship")
        status = one_of_text(row[5], {"target_committed", "committed"}, field="fork_materializations.status")
        _required_sha256(row[9], field="fork_materializations.source_snapshot_sha256")
        if status == "committed":
            self._active_fork_materializations.discard((target_session_id, target_thread_id, checkpoint_ns))
            return fork_id
        if relationship == "pinned":
            self._retain_fork_source(
                source_session_id=source_session_id,
                source_checkpoint_id=source_checkpoint_id,
                source_view_id=source_view_id,
                fork_id=fork_id,
                owner_session_id=target_session_id,
                checkpoint_ns=checkpoint_ns,
            )
        with (
            self._lock(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ),
            self._connect(
                target_thread_id, checkpoint_ns, session_id=target_session_id
            ) as connection,
        ):
            result = connection.execute(
                "UPDATE fork_materializations SET status = 'committed', committed_at = ?, error_message = NULL WHERE materialization_id = ? AND status = 'target_committed'",
                (_now(), materialization_id),
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"fork materialization 未进入 committed: {materialization_id}"
                )
        self._active_fork_materializations.discard(
            (target_session_id, target_thread_id, checkpoint_ns)
        )
        return fork_id

    def read_committed_fork_materialization(
        self,
        *,
        target_session_id: str,
        source_session_id: str,
        source_thread_id: str,
        target_thread_id: str,
        source_checkpoint_id: str | None,
        source_view_id: str | None,
        fork_mode: str,
        relationship: str,
        checkpoint_ns: str,
        fork_id: str,
        materialization_id: str,
        source_snapshot_sha256: str,
    ) -> tuple[str, str]:
        """从唯一 committed journal 复核完整 source/target capture 合同。"""
        target_session_id = required_text(target_session_id, field="target_session_id")
        source_session_id = required_text(source_session_id, field="source_session_id")
        source_thread_id = required_text(source_thread_id, field="source_thread_id")
        target_thread_id = required_text(target_thread_id, field="target_thread_id")
        source_checkpoint_id = optional_text(
            source_checkpoint_id, field="source_checkpoint_id"
        )
        source_view_id = optional_text(source_view_id, field="source_view_id")
        fork_id = required_text(fork_id, field="fork_id")
        materialization_id = required_text(
            materialization_id, field="materialization_id"
        )
        source_snapshot_sha256 = _required_sha256(
            source_snapshot_sha256, field="source_snapshot_sha256"
        )
        if not isinstance(checkpoint_ns, str):
            raise TypeError("checkpoint_ns 必须是字符串")
        if self.main_thread_id(target_session_id) != target_thread_id:
            raise RuntimeError("fork proof target thread 与 target owner 不一致")
        self.initialize(
            target_session_id,
            checkpoint_ns,
            thread_id=target_thread_id,
        )
        with (
            self._lock(
                target_thread_id,
                checkpoint_ns,
                session_id=target_session_id,
            ),
            self._connect(
                target_thread_id,
                checkpoint_ns,
                session_id=target_session_id,
                read_only=True,
            ) as connection,
        ):
            rows = connection.execute(
                "SELECT materialization_id, fork_id, source_session_id, "
                "target_session_id, source_checkpoint_id, source_view_id, fork_mode, "
                "relationship, status, source_thread_id, target_thread_id, "
                "checkpoint_ns, source_snapshot_sha256 "
                "FROM fork_materializations WHERE target_session_id = ? "
                "AND status = 'committed' ORDER BY created_at, materialization_id",
                (target_session_id,),
            ).fetchall()
            if len(rows) != 1:
                raise RuntimeError(
                    "fork proof 要求 target 只有一条 committed materialization journal: "
                    f"target_session_id={target_session_id}, count={len(rows)}"
                )
            row = rows[0]
            actual = (
                required_text(row[0], field="fork_materializations.materialization_id"),
                required_text(row[1], field="fork_materializations.fork_id"),
                required_text(row[2], field="fork_materializations.source_session_id"),
                required_text(row[3], field="fork_materializations.target_session_id"),
                optional_text(row[4], field="fork_materializations.source_checkpoint_id"),
                optional_text(row[5], field="fork_materializations.source_view_id"),
                one_of_text(
                    row[6],
                    {"context_fork", "history_prefix_fork", "full_rollout_copy"},
                    field="fork_materializations.fork_mode",
                ),
                one_of_text(
                    row[7],
                    {"detached", "pinned"},
                    field="fork_materializations.relationship",
                ),
                one_of_text(
                    row[8], {"committed"}, field="fork_materializations.status"
                ),
                required_text(row[9], field="fork_materializations.source_thread_id"),
                required_text(row[10], field="fork_materializations.target_thread_id"),
                strict_text(
                    row[11],
                    field="fork_materializations.checkpoint_ns",
                    allow_empty=True,
                ),
                _required_sha256(
                    row[12], field="fork_materializations.source_snapshot_sha256"
                ),
            )
        expected = (
            materialization_id,
            fork_id,
            source_session_id,
            target_session_id,
            source_checkpoint_id,
            source_view_id,
            fork_mode,
            relationship,
            "committed",
            source_thread_id,
            target_thread_id,
            checkpoint_ns,
            source_snapshot_sha256,
        )
        if actual != expected:
            raise RuntimeError(
                "committed fork journal 与 source/target snapshot 合同不一致: "
                f"expected={expected!r}, actual={actual!r}"
            )
        return actual[0], actual[1]
