"""v2 fork completion/clone owner。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from uuid import uuid4

from app.domain.itemized.enums import (
    CommitKind,
    ControlOutcome,
    SemanticKind,
    TurnStatus,
)
from app.domain.itemized.execution_admission import user_turn_admission_payload_hash
from app.domain.itemized.hashing import validate_hash_token
from app.domain.itemized.records import parse_item_projection_manifest
from app.services.infrastructure.rollout_context.fork.cloning import ForkCloneMixin
from app.services.infrastructure.rollout_context.fork.identity import (
    target_local_acceptance_identity,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    json_mapping,
    non_negative_int,
    one_of_text,
    optional_text,
    required_text,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_idempotency_commit,
    read_committed_item_identities,
)


class ForkCompletionMixin(ForkCloneMixin):
    """fork 后 Turn finalization、v2 turn lineage 与 clone owner。"""

    def _ensure_fork_identity_mapping(
        self,
        connection: sqlite3.Connection,
        *,
        source_session_id: str,
        target_session_id: str,
        fork_id: str,
        entity_type: str,
        source_id: str,
        target_id: str,
        lineage: dict[str, object],
        timestamp: str,
    ) -> None:
        lineage_json = _json(lineage)
        existing = connection.execute(
            "SELECT source_session_id, target_session_id, target_local_id, lineage_json "
            "FROM fork_identity_mappings WHERE fork_id = ? AND entity_type = ? "
            "AND source_local_id = ?",
            (fork_id, entity_type, source_id),
        ).fetchone()
        if existing is not None:
            stored = (
                required_text(existing[0], field="fork_identity_mappings.source_session_id"),
                required_text(existing[1], field="fork_identity_mappings.target_session_id"),
                required_text(existing[2], field="fork_identity_mappings.target_local_id"),
                json_mapping(existing[3], field="fork_identity_mappings.lineage_json"),
            )
            if stored != (
                source_session_id,
                target_session_id,
                target_id,
                lineage,
            ):
                raise RuntimeError(
                    "fork identity mapping 与已提交 source/target lineage 不一致: "
                    f"fork_id={fork_id} entity_type={entity_type} source_id={source_id}"
                )
            return
        target_collision = connection.execute(
            "SELECT source_session_id, fork_id FROM fork_identity_mappings "
            "WHERE target_session_id = ? AND entity_type = ? AND target_local_id = ?",
            (target_session_id, entity_type, target_id),
        ).fetchone()
        if target_collision is not None:
            raise RuntimeError(
                "fork identity target 已被其他 source/fork 占用: "
                f"target_session_id={target_session_id} entity_type={entity_type} "
                f"target_id={target_id}"
            )
        result = connection.execute(
            "INSERT INTO fork_identity_mappings(mapping_id, fork_id, source_session_id, "
            "target_session_id, entity_type, source_local_id, target_local_id, "
            "source_offset, target_offset, lineage_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (
                uuid4().hex,
                fork_id,
                source_session_id,
                target_session_id,
                entity_type,
                source_id,
                target_id,
                lineage_json,
                timestamp,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                "fork identity mapping 未写入: "
                f"fork_id={fork_id} entity_type={entity_type} source_id={source_id}"
            )

    def _fork_completed_turn_finalizations(
        self,
        source_session_id: str,
        source_checkpoint_id: str | None,
        checkpoint_ns: str,
    ) -> list[tuple[str, str]]:
        from app.services.infrastructure.rollout_context.storage.primitives import (
            _RolloutFileLock,
        )

        source_session_id = required_text(
            source_session_id, field="completed_finalizations.source_session_id"
        )
        source_thread_id = self.main_thread_id(source_session_id)
        source_checkpoint_id = optional_text(
            source_checkpoint_id,
            field="completed_finalizations.source_checkpoint_id",
        )
        if not isinstance(checkpoint_ns, str):
            raise TypeError("completed_finalizations.checkpoint_ns 必须是字符串")
        source_root = self.root(
            source_session_id, checkpoint_ns, thread_id=source_thread_id
        )
        if not source_root.is_dir():
            return []
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(
                source_thread_id,
                checkpoint_ns,
                session_id=source_session_id,
            ) as connection:
                self._require_v2_runtime(connection)
                if source_checkpoint_id is not None:
                    source_view_row = connection.execute(
                        "SELECT view_id FROM checkpoints WHERE checkpoint_id = ? AND checkpoint_ns = ? AND status = 'active'",
                        (source_checkpoint_id, checkpoint_ns),
                    ).fetchone()
                    if source_view_row is None:
                        raise KeyError(source_checkpoint_id)
                    source_view_id = required_text(
                        source_view_row[0],
                        field="completed_finalizations.source_view_id",
                    )
                else:
                    source_view_row = connection.execute(
                        "SELECT head_view_id FROM branches WHERE branch_id = (SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?)",
                        (checkpoint_ns,),
                    ).fetchone()
                    source_view_id = (
                        required_text(
                            source_view_row[0],
                            field="completed_finalizations.source_view_id",
                        )
                        if source_view_row is not None
                        and source_view_row[0] is not None
                        else None
                    )
                if source_view_id is None:
                    return []
                rows = connection.execute(
                    """
                    SELECT DISTINCT t.turn_id, t.final_message_id
                    FROM context_view_turns AS cvt
                    JOIN turns AS t ON t.turn_id = cvt.turn_id
                    WHERE cvt.view_id = ?
                      AND t.status IN ('completed', 'succeeded')
                      AND t.final_message_id IS NOT NULL
                    ORDER BY t.turn_ordinal
                    """,
                    (source_view_id,),
                ).fetchall()
                return [
                    (
                        required_text(
                            turn_id,
                            field="completed_finalizations.turn_id",
                        ),
                        required_text(
                            message_id,
                            field="completed_finalizations.final_message_id",
                        ),
                    )
                    for turn_id, message_id in rows
                ]
        finally:
            source_lock.release()

    def _fork_v2_turn_records(
        self,
        source_session_id: str,
        checkpoint_ns: str,
    ) -> tuple[tuple[object, ...], ...]:
        from app.services.infrastructure.rollout_context.storage.primitives import (
            _RolloutFileLock,
        )

        """读取 source v2 Turn/acceptance 的只读 lineage 输入。"""
        source_thread_id = self.main_thread_id(source_session_id)
        source_root = self.root(
            source_session_id, checkpoint_ns, thread_id=source_thread_id
        )
        if not source_root.is_dir():
            return ()
        source_lock = _RolloutFileLock(
            source_root.parent / ".rollout.write.lock",
            exclusive=False,
        )
        source_lock.acquire()
        try:
            with self._connect(
                source_thread_id,
                checkpoint_ns,
                session_id=source_session_id,
                read_only=True,
            ) as connection:
                self._require_v2_runtime(connection)
                for table in ("turn_records", "turn_acceptances", "executions"):
                    other_owner = connection.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        "WHERE session_id != ? OR thread_id != ?",
                        (source_session_id, source_thread_id),
                    ).fetchone()
                    if other_owner is None or other_owner[0] != 0:
                        raise RuntimeError(
                            "fork source pair-owned data 含非当前 owner: "
                            f"table={table} source={(source_session_id, source_thread_id)!r}"
                        )
                rows = tuple(
                    connection.execute(
                        """
                        SELECT tr.session_id, tr.thread_id,
                               tr.turn_id, tr.turn_ordinal, tr.source_branch_id,
                               tr.root_input_item_id, tr.accepted_ingress_id,
                               tr.acceptance_idempotency_key, tr.initial_execution_id,
                               tr.status, tr.final_item_id, tr.replay_of_turn_id,
                               ta.session_id, ta.thread_id, ta.turn_id,
                               ta.accepted_ingress_id, ta.acceptance_idempotency_key,
                               ta.payload_hash,
                               e.session_id, e.thread_id, e.turn_id, e.execution_id,
                               e.execution_origin, e.job_id,
                               e.admission_idempotency_key, e.admission_payload_hash,
                               e.accepted_ingress_id, e.outcome
                        FROM turn_records AS tr
                        LEFT JOIN turn_acceptances AS ta
                          ON ta.session_id = tr.session_id
                         AND ta.thread_id = tr.thread_id
                         AND ta.turn_id = tr.turn_id
                        LEFT JOIN executions AS e
                          ON e.session_id = tr.session_id
                         AND e.thread_id = tr.thread_id
                         AND e.execution_id = tr.initial_execution_id
                        WHERE tr.session_id = ? AND tr.thread_id = ?
                        ORDER BY tr.turn_ordinal
                        """
                        ,
                        (source_session_id, source_thread_id),
                    ).fetchall()
                )
                for row in rows:
                    if len(row) != 28:
                        raise RuntimeError("fork source Turn manifest 字段数量不一致")
                    source_owner = (
                        required_text(row[0], field="source.session_id"),
                        required_text(row[1], field="source.thread_id"),
                    )
                    if source_owner != (source_session_id, source_thread_id):
                        raise RuntimeError("fork source Turn owner 与读取 pair 不一致")
                    turn_id = required_text(row[2], field="source.turn_id")
                    non_negative_int(row[3], field="source.turn_ordinal")
                    required_text(row[4], field="source.source_branch_id")
                    required_text(row[5], field="source.root_input_item_id")
                    accepted_ingress_id = required_text(
                        row[6], field="source.accepted_ingress_id"
                    )
                    acceptance_key = required_text(
                        row[7], field="source.acceptance_idempotency_key"
                    )
                    execution_id = required_text(
                        row[8], field="source.initial_execution_id"
                    )
                    one_of_text(
                        row[9],
                        {item.value for item in TurnStatus},
                        field="source.status",
                    )
                    optional_text(row[10], field="source.final_item_id")
                    optional_text(row[11], field="source.replay_of_turn_id")
                    acceptance_owner = (
                        required_text(row[12], field="source.acceptance.session_id"),
                        required_text(row[13], field="source.acceptance.thread_id"),
                    )
                    acceptance_turn_id = required_text(
                        row[14], field="source.acceptance.turn_id"
                    )
                    acceptance_ingress_id = required_text(
                        row[15], field="source.acceptance.accepted_ingress_id"
                    )
                    stored_acceptance_key = required_text(
                        row[16], field="source.acceptance.acceptance_idempotency_key"
                    )
                    payload_hash = validate_hash_token(
                        row[17], "source.acceptance.payload_hash"
                    )
                    if (
                        acceptance_owner != (source_session_id, source_thread_id)
                        or acceptance_turn_id != turn_id
                        or acceptance_ingress_id != accepted_ingress_id
                        or stored_acceptance_key != acceptance_key
                    ):
                        raise RuntimeError(
                            f"fork source Turn 与 acceptance 不一致: turn_id={turn_id}"
                        )
                    execution_owner = (
                        required_text(row[18], field="source.execution.session_id"),
                        required_text(row[19], field="source.execution.thread_id"),
                    )
                    execution_turn_id = required_text(
                        row[20], field="source.execution.turn_id"
                    )
                    stored_execution_id = required_text(
                        row[21], field="source.execution.execution_id"
                    )
                    execution_origin = required_text(
                        row[22], field="source.execution.execution_origin"
                    )
                    job_id = required_text(row[23], field="source.execution.job_id")
                    admission_key = required_text(
                        row[24], field="source.execution.admission_idempotency_key"
                    )
                    admission_hash = validate_hash_token(
                        row[25], "source.execution.admission_payload_hash"
                    )
                    execution_ingress_id = required_text(
                        row[26], field="source.execution.accepted_ingress_id"
                    )
                    one_of_text(
                        row[27],
                        {item.value for item in ControlOutcome},
                        field="source.execution.outcome",
                    )
                    expected_admission_hash = user_turn_admission_payload_hash(
                        session_id=source_session_id,
                        thread_id=source_thread_id,
                        turn_id=turn_id,
                        execution_id=execution_id,
                        job_id=job_id,
                        accepted_ingress_id=accepted_ingress_id,
                        admission_idempotency_key=admission_key,
                        payload_hash=payload_hash,
                    )
                    if (
                        execution_owner != (source_session_id, source_thread_id)
                        or execution_turn_id != turn_id
                        or stored_execution_id != execution_id
                        or execution_origin != "user_turn"
                        or execution_ingress_id != accepted_ingress_id
                        or admission_hash != expected_admission_hash
                    ):
                        raise RuntimeError(
                            "fork source execution 缺少匹配的真实 user_turn admission: "
                            f"turn_id={turn_id} execution_id={execution_id}"
                        )
                return rows
        finally:
            source_lock.release()

    def _validate_fork_internal_admission(
        self, connection: sqlite3.Connection, *, session_id: str, thread_id: str,
        checkpoint_ns: str, execution_id: str, job_id: str, admission_key: str,
        admission_hash: str,
    ) -> None:
        """所有 fork 模式共用冻结 notice 的原始 admission commit 校验。"""
        imported = connection.execute(
            "SELECT m.lineage_json, j.status, j.target_session_id, j.target_thread_id, "
            "j.checkpoint_ns, j.source_snapshot_sha256 FROM fork_identity_mappings AS m "
            "JOIN fork_materializations AS j ON j.fork_id = m.fork_id "
            "WHERE m.entity_type = 'execution' AND m.target_session_id = ? AND m.target_local_id = ?",
            (session_id, execution_id),
        ).fetchall()
        if imported:
            if len(imported) != 1 or imported[0][1:5] != ("committed", session_id, thread_id, checkpoint_ns):
                raise RuntimeError("fork source imported internal execution 缺少唯一本地 committed journal")
            lineage = json_mapping(imported[0][0], field="source.internal.imported.lineage")
            target = lineage.get("target")
            source = lineage.get("source")
            if not isinstance(target, dict) or not isinstance(source, dict):
                raise RuntimeError("fork source imported internal execution 缺少 source/target preimage")
            expected = {"session_id": session_id, "thread_id": thread_id, "execution_id": execution_id,
                        "execution_origin": "internal", "admission_idempotency_key": admission_key,
                        "admission_payload_hash": admission_hash}
            job_ref = target.get("job_ref")
            digest = imported[0][5]
            if (any(target.get(key) != value for key, value in expected.items())
                or source.get("admission_payload_hash") != admission_hash
                or not isinstance(job_ref, dict) or job_ref.get("type") != "GlobalEntityRef"
                or job_ref.get("local_id") != job_id
                or target.get("turn_id") is not None or target.get("accepted_ingress_id") is not None
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
                raise RuntimeError("fork source imported internal admission 与本地冻结 journal 不一致")
            validate_hash_token(admission_hash, "source.internal.imported.admission_hash")
            return
        metadata_rows = connection.execute(
            "SELECT metadata_json FROM storage_commits WHERE commit_kind = ? "
            "AND subject_id = ? AND idempotency_key = ?",
            (CommitKind.ITEM_CONVERGENCE.value, execution_id, f"internal-admission:{admission_key}"),
        ).fetchall()
        if len(metadata_rows) != 1:
            raise RuntimeError("fork source internal execution 缺少唯一 notice admission commit")
        metadata = json_mapping(metadata_rows[0][0], field="source.internal.admission.metadata")
        notice_id = required_text(metadata.get("notice_item_id"), field="source.internal.notice_item_id")
        notice = self._read_canonical_item_from_catalog(
            connection, session_id=session_id, thread_id=thread_id,
            checkpoint_ns=checkpoint_ns, item_id=notice_id,
        )
        binding = connection.execute(
            "SELECT execution_id FROM item_catalog WHERE item_id = ?", (notice_id,),
        ).fetchone()
        commit = load_committed_idempotency_commit(
            connection, commit_kind=CommitKind.ITEM_CONVERGENCE.value,
            subject_id=execution_id, idempotency_key=f"internal-admission:{admission_key}",
            commit_mode="item_bearing", outcome=None,
            metadata={"execution_id": execution_id, "job_id": job_id, "notice_item_id": notice_id},
            item_count=1,
        )
        if (notice.semantic_kind != SemanticKind.RUNTIME_NOTICE or notice.turn_id is not None
            or binding != (execution_id,) or commit is None
            or read_committed_item_identities(connection, commit_id=commit.commit_id)
            != ((notice_id, notice.content_hash, notice.item_sequence),)):
            raise RuntimeError("fork source internal notice 与冻结 admission commit 不一致")
        if notice.content_hash != validate_hash_token(admission_hash, "source.internal.admission_hash"):
            raise RuntimeError("fork source internal admission hash 与冻结 notice 不一致")

    def _ensure_fork_v2_turn_records(
        self, connection: sqlite3.Connection, *, source_session_id: str,
        target_session_id: str, target_thread_id: str, fork_id: str,
        checkpoint_ns: str, source_turn_rows: Sequence[tuple[object, ...]], timestamp: str,
    ) -> None:
        """prepared 事务消费冻结事实，建立真实 Turn、完整 execution 与 target mapping。"""
        source_by_turn = {}
        for row in source_turn_rows:
            if len(row) != 28:
                raise RuntimeError("fork source Turn manifest 字段数量不一致")
            turn_id = required_text(row[2], field="source.turn_id")
            if turn_id in source_by_turn:
                raise RuntimeError(f"fork source Turn manifest 重复: {turn_id}")
            source_by_turn[turn_id] = row
        source_thread_id = self.main_thread_id(source_session_id)
        item_map: dict[str, str] = {}
        turn_map: dict[str, str] = {}
        root_map: dict[str, tuple[object, ...]] = {}
        for item_id, turn_id, sequence, content_hash, scope, projection_manifest_json in connection.execute(
            "SELECT item_id, turn_id, item_sequence, content_hash, turn_scope, projection_manifest_json "
            "FROM item_catalog ORDER BY item_sequence"
        ).fetchall():
            manifest = parse_item_projection_manifest(json_mapping(projection_manifest_json, field="target.item.projection_manifest_json"))
            origins = [relation for relation in manifest["provenance_relations"]
                       if relation["relation"] == "derived_from" and relation["target_kind"] == "canonical_item"
                       and relation["target_session_id"] == source_session_id and relation["target_thread_id"] == source_thread_id]
            if len(origins) != 1:
                raise RuntimeError("fork canonical item 缺少唯一 typed source origin")
            source_item_id = required_text(origins[0]["target_id"], field="fork_source_item_id")
            item_map[source_item_id] = item_id
            source_turn_id = optional_text(origins[0].get("target_turn_id"), field="fork_source_turn_id")
            if source_turn_id is not None:
                if turn_id is None or turn_map.setdefault(source_turn_id, turn_id) != turn_id:
                    raise RuntimeError("fork source Turn 到 target Turn 的 mapping 不唯一")
                if scope == "turn_root":
                    if source_turn_id in root_map:
                        raise RuntimeError("fork source Turn 映射到多个 canonical root")
                    root_map[source_turn_id] = (item_id, sequence, content_hash)
        if set(turn_map) != set(root_map):
            raise RuntimeError("fork copied Turn 缺少真实 canonical root")
        branch = connection.execute(
            "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
            (checkpoint_ns,),
        ).fetchone()
        if branch is None:
            raise RuntimeError("fork target active branch 缺失")
        target_branch = required_text(branch[0], field="target.active_branch_id")
        with self._connect(source_thread_id, checkpoint_ns, session_id=source_session_id,
                           read_only=True) as source_connection:
            used_executions = {execution_id for item_id, execution_id in source_connection.execute(
                "SELECT item_id, execution_id FROM item_catalog"
            ).fetchall() if item_id in item_map}
            all_execution_rows = source_connection.execute(
                "SELECT execution_id, session_id, thread_id, turn_id, execution_origin, job_id, "
                "admission_idempotency_key, admission_payload_hash, attempt, execution_ordinal, "
                "accepted_ingress_id, outcome, resumed_from_execution_id, replay_of_execution_id, "
                "first_model_call_id, last_model_call_id, created_at, updated_at FROM executions "
                "ORDER BY execution_ordinal, execution_id"
            ).fetchall()
            selected_ordinals = {
                turn_id: max((row[9] for row in all_execution_rows
                              if row[3] == turn_id and row[0] in used_executions), default=1)
                for turn_id in turn_map
            }
            execution_rows = tuple(row for row in all_execution_rows
                                   if (row[3] in selected_ordinals and row[9] <= selected_ordinals[row[3]])
                                   or row[0] in used_executions)
            execution_map = {row[0]: self._full_copy_identity(fork_id, target_session_id, "execution", row[0])
                             for row in execution_rows}
            if used_executions - set(execution_map):
                raise RuntimeError("fork canonical item 引用了缺失的 source execution")
            model_rows = tuple(row for row in source_connection.execute(
                "SELECT model_call_id, session_id, thread_id, execution_id, attempt, attempt_ordinal, "
                "provider, provider_request_id, retry_of_model_call_id, assembly_id, outcome, created_at "
                "FROM model_calls ORDER BY attempt_ordinal, model_call_id"
            ).fetchall() if row[3] in execution_map)
            model_map = {row[0]: self._full_copy_identity(fork_id, target_session_id, "model_call", row[0])
                         for row in model_rows}
            source_last_execution = dict(source_connection.execute(
                "SELECT turn_id, last_execution_id FROM turn_records"
            ).fetchall())
            source_links = tuple(row for row in source_connection.execute(
                "SELECT turn_id, execution_id, execution_role, execution_ordinal, link_idempotency_key "
                "FROM turn_execution_links WHERE session_id = ? AND thread_id = ?",
                (source_session_id, source_thread_id),
            ).fetchall() if row[0] in turn_map and row[1] in execution_map)
            for row in execution_rows:
                if row[4] != "internal":
                    continue
                self._validate_fork_internal_admission(
                    source_connection, session_id=source_session_id, thread_id=source_thread_id,
                    checkpoint_ns=checkpoint_ns, execution_id=row[0], job_id=row[5],
                    admission_key=row[6], admission_hash=row[7],
                )
        copied_last_execution = {row[3]: execution_map[row[0]] for row in execution_rows if row[3] is not None}
        acceptance_map: dict[str, tuple[str, str]] = {}

        def mapping(entity_type: str, source_id: str, target_id: str,
                    lineage: dict[str, object] | None = None) -> None:
            self._ensure_fork_identity_mapping(
                connection, source_session_id=source_session_id, target_session_id=target_session_id,
                fork_id=fork_id, entity_type=entity_type, source_id=source_id, target_id=target_id,
                lineage=lineage or {"source": {"session_id": source_session_id, "thread_id": source_thread_id,
                                               "local_id": source_id},
                                    "target": {"session_id": target_session_id, "thread_id": target_thread_id,
                                               "local_id": target_id}}, timestamp=timestamp,
            )

        for ordinal, source_turn_id in enumerate(turn_map, 1):
            source = source_by_turn.get(source_turn_id)
            if source is None:
                raise RuntimeError(f"fork source 缺少真实 Turn/admission lineage: {source_turn_id}")
            target_turn_id = turn_map[source_turn_id]
            root_item, root_sequence, root_hash = root_map[source_turn_id]
            if source[5] not in item_map or item_map[source[5]] != root_item or source[17] != root_hash:
                raise RuntimeError("fork target canonical root 与冻结 source acceptance 不一致")
            ingress, key = target_local_acceptance_identity(
                target_session_id=target_session_id, fork_id=fork_id,
                source_accepted_ingress_id=source[6], source_acceptance_key=source[7],
            )
            acceptance_map[source_turn_id] = (ingress, key)
            status = source[9]
            final_item = item_map.get(source[10]) if source[10] is not None else None
            if status == TurnStatus.COMPLETED.value and final_item is None:
                status = TurnStatus.UNKNOWN.value
            connection.execute(
                "INSERT INTO turn_acceptances(accepted_ingress_id, acceptance_idempotency_key, session_id, "
                "thread_id, turn_id, payload_hash, source_session_id, source_accepted_ingress_id, "
                "source_acceptance_idempotency_key, identity_origin, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'fork_copied', ?)",
                (ingress, key, target_session_id, target_thread_id, target_turn_id, root_hash,
                 source_session_id, source[6], source[7], timestamp),
            )
            connection.execute(
                "INSERT INTO turn_records(turn_id, session_id, thread_id, turn_ordinal, source_branch_id, "
                "root_input_item_id, root_input_item_sequence, accepted_ingress_id, acceptance_idempotency_key, "
                "initial_execution_id, last_execution_id, status, final_item_id, replay_of_turn_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (target_turn_id, target_session_id, target_thread_id, ordinal, target_branch,
                 root_item, root_sequence, ingress, key, execution_map[source[8]],
                 copied_last_execution[source_turn_id], status, final_item,
                 turn_map.get(source[11]), timestamp, timestamp),
            )
            mapping("turn", source_turn_id, target_turn_id,
                    {"source": {"session_id": source_session_id, "thread_id": source_thread_id,
                                "turn_id": source_turn_id, "turn_ordinal": source[3],
                                "source_branch_id": source[4], "root_input_item_id": source[5],
                                "accepted_ingress_id": source[6], "acceptance_idempotency_key": source[7],
                                "initial_execution_id": source[8],
                                "last_execution_id": source_last_execution[source_turn_id],
                                "status": source[9], "final_item_id": source[10], "replay_of_turn_id": source[11]},
                     "target": {"session_id": target_session_id, "thread_id": target_thread_id,
                                "turn_id": target_turn_id, "turn_ordinal": ordinal,
                                "source_branch_id": target_branch, "root_input_item_id": root_item,
                                "accepted_ingress_id": ingress, "acceptance_idempotency_key": key,
                                "initial_execution_id": execution_map[source[8]],
                                "last_execution_id": copied_last_execution[source_turn_id],
                                "status": status, "final_item_id": final_item}})
            mapping("accepted_ingress", source[6], ingress)
            mapping("acceptance_idempotency_key", source[7], key)
        for row in execution_rows:
            (execution_id, owner_session, owner_thread, turn_id, origin, job_id, admission_key,
             admission_hash, attempt, execution_ordinal, ingress, outcome, resumed, replay,
             first_model, last_model, created_at, updated_at) = row
            if (owner_session, owner_thread) != (source_session_id, source_thread_id):
                raise RuntimeError("fork source execution owner 不一致")
            validate_hash_token(admission_hash, "source.execution.admission_payload_hash")
            if origin == "user_turn":
                source_turn = source_by_turn[turn_id]
                source_hash = user_turn_admission_payload_hash(
                    session_id=source_session_id, thread_id=source_thread_id, turn_id=turn_id,
                    execution_id=execution_id, job_id=job_id, accepted_ingress_id=ingress,
                    admission_idempotency_key=admission_key, payload_hash=source_turn[17],
                )
                if ingress != source_turn[6] or admission_hash != source_hash:
                    raise RuntimeError("fork source execution admission preimage 不一致")
            elif origin != "internal" or turn_id is not None or ingress is not None:
                raise RuntimeError("fork source execution origin/Turn 关系不一致")
            target_execution = execution_map[execution_id]
            target_key = self._full_copy_identity(fork_id, target_session_id, "admission_idempotency_key", admission_key)
            target_turn = turn_map.get(turn_id)
            target_ingress = acceptance_map[turn_id][0] if turn_id is not None else None
            target_hash = (user_turn_admission_payload_hash(
                session_id=target_session_id, thread_id=target_thread_id, turn_id=target_turn,
                execution_id=target_execution, job_id=job_id, accepted_ingress_id=target_ingress,
                admission_idempotency_key=target_key, payload_hash=source_by_turn[turn_id][17],
            ) if origin == "user_turn" else admission_hash)
            connection.execute(
                "INSERT INTO executions(execution_id, session_id, thread_id, turn_id, execution_origin, job_id, "
                "admission_idempotency_key, admission_payload_hash, attempt, execution_ordinal, accepted_ingress_id, "
                "outcome, resumed_from_execution_id, replay_of_execution_id, first_model_call_id, last_model_call_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (target_execution, target_session_id, target_thread_id, target_turn, origin, job_id, target_key,
                 target_hash, attempt, execution_ordinal, target_ingress, outcome, execution_map.get(resumed),
                 execution_map.get(replay), model_map.get(first_model), model_map.get(last_model), created_at, updated_at),
            )
            job_ref = {"type": "GlobalEntityRef", "session_id": source_session_id, "thread_id": source_thread_id,
                       "entity_type": "job", "local_id": job_id}
            mapping("execution", execution_id, target_execution,
                    {"source": {"session_id": source_session_id, "thread_id": source_thread_id,
                                "execution_id": execution_id, "job_ref": job_ref,
                                "execution_origin": origin, "attempt": attempt, "execution_ordinal": execution_ordinal,
                                "admission_idempotency_key": admission_key, "admission_payload_hash": admission_hash,
                                "resumed_from_execution_id": resumed, "replay_of_execution_id": replay},
                     "target": {"session_id": target_session_id, "thread_id": target_thread_id,
                                "execution_id": target_execution, "job_ref": job_ref,
                                "execution_origin": origin, "attempt": attempt, "execution_ordinal": execution_ordinal,
                                "admission_idempotency_key": target_key, "admission_payload_hash": target_hash,
                                "resumed_from_execution_id": execution_map.get(resumed),
                                "replay_of_execution_id": execution_map.get(replay)}})
            mapping("admission_idempotency_key", admission_key, target_key)
        for turn_id, execution_id, role, ordinal, link_key in source_links:
            if execution_id not in execution_map:
                raise RuntimeError("fork source Turn link 引用了缺失的 execution")
            connection.execute(
                "INSERT INTO turn_execution_links(session_id, thread_id, turn_id, execution_id, execution_role, "
                "execution_ordinal, link_idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (target_session_id, target_thread_id, turn_map[turn_id], execution_map[execution_id],
                 "fork_copied", ordinal, self._full_copy_identity(fork_id, target_session_id, "execution_link", link_key), timestamp),
            )
        for row in model_rows:
            (model_id, owner_session, owner_thread, execution_id, attempt, ordinal, provider, provider_request,
             retry_of, assembly_id, outcome, created_at) = row
            if (owner_session, owner_thread) != (source_session_id, source_thread_id):
                raise RuntimeError("fork source model call owner 不一致")
            connection.execute(
                "INSERT INTO model_calls(model_call_id, session_id, thread_id, execution_id, attempt, attempt_ordinal, "
                "provider, provider_request_id, retry_of_model_call_id, assembly_id, dispatch_state, outcome, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'failed', ?, ?)",
                (model_map[model_id], target_session_id, target_thread_id, execution_map[execution_id],
                 attempt, ordinal, provider, provider_request, model_map.get(retry_of), outcome, created_at),
            )
            mapping("model_call", model_id, model_map[model_id],
                    {"source": {"session_id": source_session_id, "thread_id": source_thread_id,
                                "model_call_id": model_id, "assembly_id": assembly_id},
                     "target": {"session_id": target_session_id, "thread_id": target_thread_id,
                                "model_call_id": model_map[model_id]}})
        for source_item, target_item in item_map.items():
            mapping("item", source_item, target_item)
