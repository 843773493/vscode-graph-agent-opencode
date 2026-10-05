"""Execution admission 的唯一持久 owner。"""

from __future__ import annotations

import os
from dataclasses import replace

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import CommitKind, SemanticKind, TurnScope
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    user_turn_admission_payload_hash,
)
from app.domain.itemized.hashing import validate_hash_token
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_idempotency_commit,
    read_committed_item_identities,
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutExecutionAdmissionMixin:
    """从既有 Turn acceptance 或 standalone internal admission 读取准入。"""

    def admit_internal_execution(
        self,
        session_id: str,
        *,
        thread_id: str,
        execution_id: str,
        job_id: str,
        admission_idempotency_key: str,
        notice_item: CanonicalItemRecord,
        checkpoint_ns: str = "",
    ) -> ExecutionAdmission:
        """原子接受 internal execution 与首条 runtime notice。"""
        session_id = strict_text(session_id, field="session_id")
        owner_thread_id = self._resolve_thread_identity(session_id, thread_id)
        execution_id = strict_text(execution_id, field="execution_id")
        job_id = strict_text(job_id, field="job_id")
        admission_idempotency_key = strict_text(
            admission_idempotency_key,
            field="admission_idempotency_key",
        )
        if not isinstance(notice_item, CanonicalItemRecord):
            raise TypeError("internal admission notice_item 必须是 CanonicalItemRecord")
        notice_item.validate()
        if (
            notice_item.item_id.startswith("item-") is False
            or notice_item.semantic_kind != SemanticKind.RUNTIME_NOTICE
            or notice_item.turn_id is not None
            or notice_item.turn_scope != TurnScope.PENDING_NEXT_TURN
        ):
            raise ValueError(
                "internal admission 首条 item 必须是 pending_next_turn runtime_notice"
            )
        payload_hash = validate_hash_token(
            notice_item.content_hash,
            "internal admission notice content_hash",
        )
        has_display_part = (
            isinstance(notice_item.payload, dict)
            and "display_part" in notice_item.payload
        )
        commit_metadata = {
            "execution_id": execution_id,
            "job_id": job_id,
            "notice_item_id": notice_item.item_id,
        }
        commit_key = f"internal-admission:{admission_idempotency_key}"
        checkpoint_ns = strict_text(
            checkpoint_ns,
            field="checkpoint_ns",
            allow_empty=True,
        )
        with self._lock(owner_thread_id, checkpoint_ns, session_id=session_id):
            self.initialize(
                session_id,
                checkpoint_ns,
                thread_id=owner_thread_id,
            )
            with self._connect(
                owner_thread_id,
                checkpoint_ns,
                session_id=session_id,
            ) as connection:
                self._require_v2_runtime(connection)
                existing = connection.execute(
                    "SELECT execution_id, turn_id, execution_origin, job_id, "
                    "admission_idempotency_key, admission_payload_hash, "
                    "accepted_ingress_id FROM executions "
                    "WHERE session_id = ? AND thread_id = ? "
                    "AND admission_idempotency_key = ?",
                    (session_id, owner_thread_id, admission_idempotency_key),
                ).fetchone()
                if existing is not None:
                    stored = (
                        strict_text(existing[0], field="executions.execution_id"),
                        strict_optional_text(existing[1], field="executions.turn_id"),
                        strict_text(existing[2], field="executions.execution_origin"),
                        strict_text(existing[3], field="executions.job_id"),
                        strict_text(
                            existing[4],
                            field="executions.admission_idempotency_key",
                        ),
                        strict_text(
                            existing[5],
                            field="executions.admission_payload_hash",
                        ),
                        strict_optional_text(
                            existing[6],
                            field="executions.accepted_ingress_id",
                        ),
                    )
                    expected = (
                        execution_id,
                        None,
                        "internal",
                        job_id,
                        admission_idempotency_key,
                        payload_hash,
                        None,
                    )
                    if stored != expected:
                        raise ValueError(
                            "internal admission 幂等键已绑定不同 execution/job/preimage: "
                            f"key={admission_idempotency_key}"
                        )
                    stored_item = self._read_canonical_item_from_catalog(
                        connection,
                        session_id=session_id,
                        thread_id=owner_thread_id,
                        checkpoint_ns=checkpoint_ns,
                        item_id=notice_item.item_id,
                    )
                    expected_item = replace(
                        notice_item,
                        item_sequence=stored_item.item_sequence,
                    )
                    if stored_item != expected_item:
                        raise ValueError(
                            "internal admission 幂等键已绑定不同 runtime notice: "
                            f"item_id={notice_item.item_id}"
                        )
                    item_binding = connection.execute(
                        "SELECT execution_id FROM item_catalog WHERE item_id = ?",
                        (notice_item.item_id,),
                    ).fetchone()
                    if item_binding is None or strict_text(
                        item_binding[0],
                        field=f"item_catalog.execution_id:{notice_item.item_id}",
                    ) != execution_id:
                        raise RuntimeError(
                            "internal admission notice 缺少一致 execution binding: "
                            f"item_id={notice_item.item_id}"
                        )
                    committed = load_committed_idempotency_commit(
                        connection,
                        commit_kind=CommitKind.ITEM_CONVERGENCE.value,
                        subject_id=execution_id,
                        idempotency_key=commit_key,
                        commit_mode="item_bearing",
                        outcome=None,
                        metadata=commit_metadata,
                        item_count=1,
                    )
                    if committed is None or read_committed_item_identities(
                        connection,
                        commit_id=committed.commit_id,
                    ) != (
                        (
                            notice_item.item_id,
                            notice_item.content_hash,
                            stored_item.item_sequence,
                        ),
                    ):
                        raise RuntimeError(
                            "internal admission notice commit 缺失或 identity 不一致: "
                            f"item_id={notice_item.item_id}"
                        )
                    if has_display_part:
                        view_id = self._active_context_view_id(
                            connection,
                            checkpoint_ns,
                        )
                        if view_id is None:
                            raise RuntimeError(
                                "internal admission notice 缺少 active context view"
                            )
                        membership = connection.execute(
                            "SELECT visible, source_kind FROM context_view_items "
                            "WHERE view_id = ? AND item_id = ?",
                            (view_id, notice_item.item_id),
                        ).fetchone()
                        if (
                            membership is None
                            or membership[0] != 1
                            or strict_text(
                                membership[1],
                                field="context_view_items.source_kind",
                            )
                            != "display_only"
                        ):
                            raise RuntimeError(
                                "internal admission display notice 缺少 display_only membership: "
                                f"item_id={notice_item.item_id}"
                            )
                    return ExecutionAdmission(
                        session_id=session_id,
                        thread_id=owner_thread_id,
                        execution_id=execution_id,
                        job_id=job_id,
                        admission_idempotency_key=admission_idempotency_key,
                        execution_origin="internal",
                        turn_id=None,
                    )
                occupied_execution = connection.execute(
                    "SELECT execution_origin, admission_idempotency_key "
                    "FROM executions WHERE session_id = ? AND thread_id = ? "
                    "AND execution_id = ?",
                    (session_id, owner_thread_id, execution_id),
                ).fetchone()
                if occupied_execution is not None:
                    raise ValueError(
                        "execution_id 已绑定其它 admission: "
                        f"execution_id={execution_id}"
                    )
                jsonl_path = self.jsonl_path(
                    session_id,
                    checkpoint_ns,
                    thread_id=owner_thread_id,
                )
                original_size = jsonl_path.stat().st_size
                connection.execute("BEGIN IMMEDIATE")
                try:
                    last_item_sequence = connection.execute(
                        "SELECT last_item_sequence FROM database_meta "
                        "WHERE singleton_id = 1"
                    ).fetchone()
                    if last_item_sequence is None:
                        raise RuntimeError(
                            "internal admission 缺少 database_meta.last_item_sequence"
                        )
                    sequence = strict_non_negative_int(
                        last_item_sequence[0],
                        field="database_meta.last_item_sequence",
                    ) + 1
                    committed_notice = replace(notice_item, item_sequence=sequence)
                    timestamp = _now()
                    connection.execute(
                        "INSERT INTO executions(execution_id, session_id, thread_id, "
                        "turn_id, execution_origin, job_id, admission_idempotency_key, "
                        "admission_payload_hash, attempt, execution_ordinal, "
                        "accepted_ingress_id, outcome, created_at, updated_at) "
                        "VALUES (?, ?, ?, NULL, 'internal', ?, ?, ?, 1, 1, NULL, 'unknown', ?, ?)",
                        (
                            execution_id,
                            session_id,
                            owner_thread_id,
                            job_id,
                            admission_idempotency_key,
                            payload_hash,
                            timestamp,
                            timestamp,
                        ),
                    )
                    _commit_id, _offset = self._append_v2_records_transaction(
                        connection,
                        owner_thread_id,
                        checkpoint_ns,
                        (committed_notice,),
                        session_id=session_id,
                        commit_kind=CommitKind.ITEM_CONVERGENCE.value,
                        subject_id=execution_id,
                        idempotency_key=commit_key,
                        metadata=commit_metadata,
                        begin_transaction=False,
                    )
                    self._bind_canonical_item_execution(
                        connection,
                        item_id=committed_notice.item_id,
                        execution_id=execution_id,
                    )
                    if has_display_part and not self._append_context_view_items(
                        connection,
                        checkpoint_ns=checkpoint_ns,
                        item_ids=(committed_notice.item_id,),
                        require_view=True,
                        source_kind="display_only",
                    ):
                        raise RuntimeError(
                            "internal admission display_only membership 未新增: "
                            f"item_id={committed_notice.item_id}"
                        )
                    self._commit_connection(connection)
                except BaseException as error:
                    cleanup_error: BaseException | None = None
                    if connection.in_transaction:
                        try:
                            connection.rollback()
                        # 回滚错误必须作为原异常的 cause 保留，不能替换业务失败。
                        except BaseException as rollback_error:  # noqa: BLE001
                            cleanup_error = rollback_error
                    try:
                        if jsonl_path.stat().st_size > original_size:
                            with jsonl_path.open("r+b") as stream:
                                stream.truncate(original_size)
                                stream.flush()
                                os.fsync(stream.fileno())
                    # JSONL truncate/flush/fsync 清理错误也与原失败聚合后继续上抛。
                    except BaseException as file_error:  # noqa: BLE001
                        cleanup_error = file_error
                    if cleanup_error is not None:
                        raise error from cleanup_error
                    raise
        return ExecutionAdmission(
            session_id=session_id,
            thread_id=owner_thread_id,
            execution_id=execution_id,
            job_id=job_id,
            admission_idempotency_key=admission_idempotency_key,
            execution_origin="internal",
            turn_id=None,
        )

    def execution_admission_for_job(
        self,
        session_id: str,
        *,
        thread_id: str,
        job_id: str,
        checkpoint_ns: str = "",
    ) -> ExecutionAdmission:
        """按精确 owner 和 Job 查唯一准入，不从 trace 或 main 别名补身份。"""
        session_id = strict_text(session_id, field="session_id")
        owner_thread_id = self._resolve_thread_identity(session_id, thread_id)
        job_id = strict_text(job_id, field="job_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        self.initialize(session_id, checkpoint_ns, thread_id=owner_thread_id)
        with self._connect(
            owner_thread_id,
            checkpoint_ns,
            session_id=session_id,
            read_only=True,
        ) as connection:
            self._require_v2_runtime(connection)
            rows = connection.execute(
                "SELECT execution_id FROM executions "
                "WHERE session_id = ? AND thread_id = ? AND job_id = ? "
                "ORDER BY execution_id LIMIT 2",
                (session_id, owner_thread_id, job_id),
            ).fetchall()
        owner = (
            f"session_id={session_id!r}, thread_id={owner_thread_id!r}, "
            f"job_id={job_id!r}, checkpoint_ns={checkpoint_ns!r}"
        )
        if not rows:
            raise KeyError(f"Job execution admission 不存在: {owner}")
        if len(rows) != 1:
            raise RuntimeError(f"Job execution admission 不唯一: {owner}")
        return self.execution_admission(
            session_id,
            thread_id=owner_thread_id,
            execution_id=strict_text(rows[0][0], field="executions.execution_id"),
            checkpoint_ns=checkpoint_ns,
        )

    def execution_admission(
        self,
        session_id: str,
        *,
        thread_id: str,
        execution_id: str,
        checkpoint_ns: str = "",
    ) -> ExecutionAdmission:
        """读取并核验 owner 下的 user-turn/internal execution admission。"""
        session_id = strict_text(session_id, field="session_id")
        owner_thread_id = self._resolve_thread_identity(session_id, thread_id)
        execution_id = strict_text(execution_id, field="execution_id")
        checkpoint_ns = strict_text(
            checkpoint_ns,
            field="checkpoint_ns",
            allow_empty=True,
        )
        self.initialize(
            session_id,
            checkpoint_ns,
            thread_id=owner_thread_id,
        )
        with self._connect(
            owner_thread_id,
            checkpoint_ns,
            session_id=session_id,
            read_only=True,
        ) as connection:
            self._require_v2_runtime(connection)
            row = connection.execute(
                "SELECT turn_id, execution_origin, job_id, "
                "admission_idempotency_key, admission_payload_hash, accepted_ingress_id "
                "FROM executions WHERE session_id = ? AND thread_id = ? AND execution_id = ?",
                (session_id, owner_thread_id, execution_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"execution admission 不存在: {execution_id}")
            turn_id = strict_optional_text(row[0], field="executions.turn_id")
            origin = strict_text(row[1], field="executions.execution_origin")
            job_id = strict_text(row[2], field="executions.job_id")
            admission_key = strict_text(
                row[3],
                field="executions.admission_idempotency_key",
            )
            admission_payload_hash = validate_hash_token(
                row[4],
                "executions.admission_payload_hash",
            )
            accepted_ingress_id = strict_optional_text(
                row[5],
                field="executions.accepted_ingress_id",
            )
            if origin == "internal":
                if turn_id is not None or accepted_ingress_id is not None:
                    raise RuntimeError(
                        "internal execution 不得绑定 Turn 或 accepted ingress: "
                        f"execution_id={execution_id}"
                    )
            elif origin == "user_turn":
                if turn_id is None or accepted_ingress_id is None:
                    raise RuntimeError(
                        "user_turn execution 缺少 Turn/accepted ingress: "
                        f"execution_id={execution_id}"
                    )
                acceptance = connection.execute(
                    "SELECT tr.accepted_ingress_id, ta.accepted_ingress_id, "
                    "ta.turn_id, ta.payload_hash "
                    "FROM turn_records AS tr JOIN turn_acceptances AS ta "
                    "ON ta.session_id = tr.session_id AND ta.thread_id = tr.thread_id "
                    "AND ta.turn_id = tr.turn_id "
                    "WHERE tr.session_id = ? AND tr.thread_id = ? AND tr.turn_id = ?",
                    (session_id, owner_thread_id, turn_id),
                ).fetchone()
                if acceptance is None:
                    raise RuntimeError(
                        "user_turn execution 与真实 acceptance/Turn 关系不一致: "
                        f"execution_id={execution_id}"
                    )
                turn_ingress_id = strict_text(
                    acceptance[0],
                    field="turn_records.accepted_ingress_id",
                )
                acceptance_ingress_id = strict_text(
                    acceptance[1],
                    field="turn_acceptances.accepted_ingress_id",
                )
                acceptance_turn_id = strict_text(
                    acceptance[2], field="turn_acceptances.turn_id"
                )
                acceptance_payload_hash = validate_hash_token(
                    acceptance[3],
                    "turn_acceptances.payload_hash",
                )
                if (
                    turn_ingress_id != accepted_ingress_id
                    or acceptance_ingress_id != accepted_ingress_id
                    or acceptance_turn_id != turn_id
                ):
                    raise RuntimeError(
                        "user_turn execution 与真实 acceptance/Turn 关系不一致: "
                        f"execution_id={execution_id}"
                    )
                expected_admission_hash = user_turn_admission_payload_hash(
                    session_id=session_id,
                    thread_id=owner_thread_id,
                    turn_id=turn_id,
                    execution_id=execution_id,
                    job_id=job_id,
                    accepted_ingress_id=accepted_ingress_id,
                    admission_idempotency_key=admission_key,
                    payload_hash=acceptance_payload_hash,
                )
                if admission_payload_hash != expected_admission_hash:
                    raise RuntimeError(
                        "user_turn execution admission preimage 与 acceptance 不一致: "
                        f"execution_id={execution_id}"
                    )
            else:
                raise RuntimeError(f"未知 execution_origin: {origin!r}")
        return ExecutionAdmission(
            session_id=session_id,
            thread_id=owner_thread_id,
            execution_id=execution_id,
            job_id=job_id,
            admission_idempotency_key=admission_key,
            execution_origin=origin,
            turn_id=turn_id,
        )


__all__ = ["RolloutExecutionAdmissionMixin"]
