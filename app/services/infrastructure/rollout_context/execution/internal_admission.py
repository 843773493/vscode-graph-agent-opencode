"""Internal execution admission 的唯一写事务。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import CommitKind, PayloadKind, SemanticKind, TurnScope
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    internal_execution_request_preimage,
)
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.execution.admission_reads import (
    RolloutExecutionAdmissionReadMixin,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_idempotency_commit,
    read_committed_item_identities,
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutInternalExecutionAdmissionMixin(RolloutExecutionAdmissionReadMixin):
    """Internal execution admission 的唯一写事务。"""

    def admit_internal_execution(
        self,
        session_id: str,
        *,
        thread_id: str,
        execution_id: str,
        job_id: str,
        admission_idempotency_key: str,
        runtime_notice_item: CanonicalItemRecord | None,
        request_input: Mapping[str, object],
        checkpoint_ns: str = "",
        queue_input: ExecutionQueueInput | None = None,
    ) -> ExecutionAdmission:
        """原子接受唯一 canonical request；seed/reference 不产生 notice。"""
        owner_thread_id = self._storage._resolve_thread_identity(session_id, strict_text(thread_id, field="thread_id"))
        with self._execution_input_transaction(session_id, owner_thread_id, checkpoint_ns) as connection:
            admission = self._admit_internal_execution_transaction(
                connection, session_id, thread_id=owner_thread_id,
                execution_id=execution_id, job_id=job_id,
                admission_idempotency_key=admission_idempotency_key,
                runtime_notice_item=runtime_notice_item,
                request_input=request_input, checkpoint_ns=checkpoint_ns,
            )
            if queue_input is not None:
                self.publish_execution_queue_input(connection, admission, queue_input, checkpoint_ns=checkpoint_ns)
            return admission


    @contextmanager
    def _execution_input_transaction(
        self, session_id: str, thread_id: str, checkpoint_ns: str = "",
    ) -> Iterator[sqlite3.Connection]:
        """执行输入事务共用同一 SQLite connection 与 JSONL 回滚边界。"""
        with self._storage._lock(thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(session_id, checkpoint_ns, thread_id=thread_id)
            jsonl_path = self._storage.jsonl_path(session_id, checkpoint_ns, thread_id=thread_id)
            original_size = jsonl_path.stat().st_size
            with self._storage._connect(thread_id, checkpoint_ns, session_id=session_id) as connection:
                self._storage._require_v2_runtime(connection)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                    self._storage._commit_connection(connection)
                except BaseException as error:
                    cleanup_error: BaseException | None = None
                    try:
                        connection.rollback()
                        if jsonl_path.stat().st_size > original_size:
                            with jsonl_path.open("r+b") as stream:
                                stream.truncate(original_size)
                                stream.flush()
                                os.fsync(stream.fileno())
                    except BaseException as rollback_error:  # noqa: BLE001
                        cleanup_error = rollback_error
                    if cleanup_error is not None:
                        raise error from cleanup_error
                    raise


    def _admit_internal_execution_transaction(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        *,
        thread_id: str,
        execution_id: str,
        job_id: str,
        admission_idempotency_key: str,
        runtime_notice_item: CanonicalItemRecord | None,
        request_input: Mapping[str, object],
        checkpoint_ns: str = "",
    ) -> ExecutionAdmission:
        """只消费 caller 已持有的唯一 ContextStore 事务。"""
        if not connection.in_transaction:
            raise RuntimeError("internal admission 需要活动 ContextStore 事务")
        session_id = strict_text(session_id, field="session_id")
        owner_thread_id = self._storage._resolve_thread_identity(session_id, strict_text(thread_id, field="thread_id"))
        execution_id = strict_text(execution_id, field="execution_id")
        job_id = strict_text(job_id, field="job_id")
        admission_idempotency_key = strict_text(
            admission_idempotency_key,
            field="admission_idempotency_key",
        )
        notice_item = runtime_notice_item
        if notice_item is not None:
            if not isinstance(notice_item, CanonicalItemRecord):
                raise TypeError("internal admission runtime_notice_item 必须是 CanonicalItemRecord 或 None")
            notice_item.validate()
            if (
                not notice_item.item_id.startswith("item-")
                or notice_item.semantic_kind != SemanticKind.RUNTIME_NOTICE
                or notice_item.turn_id is not None
                or notice_item.turn_scope != TurnScope.PENDING_NEXT_TURN
            ):
                raise ValueError("internal admission 首条 item 必须是 pending_next_turn runtime_notice")
            if dict(request_input) != {"kind": "runtime_notice", "payload": notice_item.payload}:
                raise ValueError("internal admission request preimage 与实际 notice 不一致")
        elif request_input.get("kind") != "seed_reference":
            raise ValueError("无 notice 的 internal admission 只允许冻结 seed/reference")
        payload_hash = sha256_jcs(internal_execution_request_preimage(
            session_id=session_id,
            thread_id=owner_thread_id,
            execution_id=execution_id,
            job_id=job_id,
            admission_idempotency_key=admission_idempotency_key,
            request_input=request_input,
        ))
        has_display_part = (
            notice_item is not None
            and notice_item.payload_kind == PayloadKind.STRUCTURED_CONTENT
            and isinstance(notice_item.payload, Mapping)
            and "display_part" in notice_item.payload
        )
        commit_metadata = {
            "execution_id": execution_id,
            "job_id": job_id,
            "notice_item_id": notice_item.item_id if notice_item is not None else None,
            "request_input": dict(request_input),
        }
        commit_key = f"internal-admission:{admission_idempotency_key}"
        checkpoint_ns = strict_text(
            checkpoint_ns,
            field="checkpoint_ns",
            allow_empty=True,
        )
        self._storage._require_v2_runtime(connection)
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
            if notice_item is None:
                if load_committed_idempotency_commit(
                    connection, commit_kind=CommitKind.EXECUTION_ADMISSION.value,
                    subject_id=execution_id, idempotency_key=commit_key,
                    commit_mode="metadata_only", outcome=None, metadata=commit_metadata, item_count=0,
                ) is None:
                    raise RuntimeError("seed/reference admission 缺少 durable request commit")
                return ExecutionAdmission(
                    session_id=session_id, thread_id=owner_thread_id,
                    execution_id=execution_id, job_id=job_id,
                    admission_idempotency_key=admission_idempotency_key,
                    execution_origin="internal", turn_id=None,
                )
            stored_item = self._storage._read_canonical_item_from_catalog(
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
                commit_kind=CommitKind.EXECUTION_ADMISSION.value,
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
                view_id = self._storage._active_context_view_id(
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
        committed_notice = (replace(notice_item, item_sequence=sequence)
                            if notice_item is not None else None)
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
        if committed_notice is not None:
            commit_id, _offset = self._storage._append_v2_records_transaction(
                connection,
                owner_thread_id,
                checkpoint_ns,
                (committed_notice,),
                session_id=session_id,
                commit_kind=CommitKind.EXECUTION_ADMISSION.value,
                subject_id=execution_id,
                idempotency_key=commit_key,
                metadata=commit_metadata,
                begin_transaction=False,
            )
            self._storage._bind_canonical_item_execution(
                connection,
                item_id=committed_notice.item_id,
                execution_id=execution_id,
            )
            message_meta = connection.execute(
                "SELECT last_message_sequence FROM database_meta WHERE singleton_id = 1"
            ).fetchone()
            if message_meta is None:
                raise RuntimeError("internal admission 缺少 message sequence 元数据")
            notice_message_sequence = strict_non_negative_int(
                message_meta[0], field="database_meta.last_message_sequence"
            ) + 1
            # 与 acceptance 共用原 projection owner；首个 checkpoint 只验证
            # 已提交 notice 的完整 group，不再次创建 canonical item。
            materialized = self._context.checkpoints._materialize_canonical_message_projection(
                connection, committed_notice, thread_id=owner_thread_id,
                session_id=session_id, checkpoint_ns=checkpoint_ns,
                commit_id=commit_id, message_sequence=notice_message_sequence,
                timestamp=timestamp,
            )
            if materialized is None or materialized[0] != notice_message_sequence:
                raise RuntimeError("internal admission notice 未生成唯一 message projection")
            commit_update = connection.execute(
                "UPDATE storage_commits SET first_message_sequence = ?, "
                "last_message_sequence = ? WHERE commit_id = ?",
                (notice_message_sequence, notice_message_sequence, commit_id),
            )
            if commit_update.rowcount != 1:
                raise RuntimeError("internal admission commit message sequence 更新失败")
            meta_update = connection.execute(
                "UPDATE database_meta SET last_message_sequence = ?, "
                "history_view_revision = history_view_revision + 1, updated_at = ? "
                "WHERE singleton_id = 1",
                (notice_message_sequence, timestamp),
            )
            if meta_update.rowcount != 1:
                raise RuntimeError("internal admission message sequence 元数据更新失败")
            if has_display_part and not self._storage._append_context_view_items(
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
        else:
            self._storage._append_v2_records_transaction(
                connection, owner_thread_id, checkpoint_ns, (), session_id=session_id,
                commit_kind=CommitKind.EXECUTION_ADMISSION.value, commit_mode="metadata_only",
                subject_id=execution_id, idempotency_key=commit_key,
                metadata=commit_metadata, begin_transaction=False,
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
