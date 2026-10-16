"""Execution admission 的唯一持久 owner。"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import CommitKind, PayloadKind, SemanticKind, TurnScope
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    ExecutionAdmissionCursor,
    internal_execution_request_preimage,
    user_turn_admission_payload_hash,
)
from app.domain.itemized.hashing import sha256_jcs, validate_hash_token
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    current_scope,
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    json_mapping,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_idempotency_commit,
    read_committed_item_identities,
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


class RolloutExecutionAdmissionMixin:
    """从既有 Turn acceptance 或 standalone internal admission 读取准入。"""

    def internal_execution_admission_preimage(
        self, connection: sqlite3.Connection, *, session_id: str, thread_id: str,
        checkpoint_ns: str, execution_id: str, job_id: str, admission_key: str,
        admission_hash: str,
    ) -> dict[str, object]:
        """本地与历史 execution 共用原始 admission commit 和原 scope 的冻结 preimage。"""
        require_historical_scope(connection, session_id, thread_id)
        metadata_rows = connection.execute(
            "SELECT metadata_json FROM storage_commits WHERE commit_kind = ? "
            "AND subject_id = ? AND idempotency_key = ?",
            (CommitKind.EXECUTION_ADMISSION.value, execution_id, f"internal-admission:{admission_key}"),
        ).fetchall()
        if len(metadata_rows) != 1:
            raise RuntimeError("fork source internal execution 缺少唯一 request admission commit")
        metadata = json_mapping(metadata_rows[0][0], field="source.internal.admission.metadata")
        request_input = metadata.get("request_input")
        if not isinstance(request_input, dict):
            raise RuntimeError("fork source internal admission 缺少冻结 request input")  # noqa: TRY004
        preimage = internal_execution_request_preimage(
            session_id=session_id, thread_id=thread_id, execution_id=execution_id,
            job_id=job_id, admission_idempotency_key=admission_key,
            request_input=request_input,
        )
        expected_hash = sha256_jcs(preimage)
        if expected_hash != validate_hash_token(admission_hash, "source.internal.admission_hash"):
            raise RuntimeError("fork source internal admission hash 与冻结 request 不一致")
        notice_id = strict_optional_text(metadata.get("notice_item_id"), field="source.internal.notice_item_id")
        is_notice = request_input["kind"] == "runtime_notice"
        if is_notice != (notice_id is not None):
            raise RuntimeError("fork source internal input kind 与 notice identity 不一致")
        commit = load_committed_idempotency_commit(
            connection, commit_kind=CommitKind.EXECUTION_ADMISSION.value,
            subject_id=execution_id, idempotency_key=f"internal-admission:{admission_key}",
            commit_mode="item_bearing" if is_notice else "metadata_only", outcome=None,
            metadata={"execution_id": execution_id, "job_id": job_id,
                      "notice_item_id": notice_id, "request_input": request_input},
            item_count=1 if is_notice else 0,
        )
        if commit is None:
            raise RuntimeError("fork source internal request 与冻结 admission commit 不一致")
        committed_items = read_committed_item_identities(connection, commit_id=commit.commit_id)
        if not is_notice:
            if committed_items:
                raise RuntimeError("fork source seed/reference admission 不得含 canonical item")
            return preimage
        storage_session, storage_thread = current_scope(connection)
        notice = self._storage._read_canonical_item_from_catalog(
            connection, session_id=storage_session, thread_id=storage_thread,
            checkpoint_ns=checkpoint_ns, item_id=notice_id,
        )
        binding = connection.execute(
            "SELECT execution_id FROM item_catalog WHERE item_id = ?", (notice_id,),
        ).fetchone()
        if (notice.semantic_kind != SemanticKind.RUNTIME_NOTICE or notice.turn_id is not None
            or notice.turn_scope != TurnScope.PENDING_NEXT_TURN
            or request_input != {"kind": "runtime_notice", "payload": notice.payload}
            or binding != (execution_id,)
            or committed_items
            != ((notice_id, notice.content_hash, notice.item_sequence),)):
            raise RuntimeError("fork source internal notice 与冻结 admission commit 不一致")
        return preimage

    def execution_admission_cursor(
        self, admission: ExecutionAdmission, *, connection: sqlite3.Connection,
        checkpoint_ns: str = "",
    ) -> ExecutionAdmissionCursor:
        """由唯一准入提交冻结 typed fence，不把 item 或任意 commit 当作准入顺序。"""
        stored = self._read_execution_admission_connection(
            connection, session_id=admission.session_id, thread_id=admission.thread_id,
            execution_id=admission.execution_id, checkpoint_ns=checkpoint_ns,
        )
        if stored != admission:
            raise ValueError("准入游标与权威 execution identity 不一致")
        if admission.execution_origin == "user_turn":
            root = connection.execute(
                "SELECT acceptance.accepted_ingress_id, acceptance.acceptance_idempotency_key, "
                "acceptance.payload_hash, turn.root_input_item_id, item.content_hash, item.item_sequence "
                "FROM turn_acceptances AS acceptance JOIN turn_records AS turn "
                "ON turn.session_id=acceptance.session_id AND turn.thread_id=acceptance.thread_id "
                "AND turn.turn_id=acceptance.turn_id LEFT JOIN item_catalog AS item "
                "ON item.item_id=turn.root_input_item_id AND item.turn_id=turn.turn_id "
                "WHERE turn.session_id=? AND turn.thread_id=? AND turn.turn_id=?",
                (admission.session_id, admission.thread_id, admission.turn_id),
            ).fetchone()
            if root is None or root[2] != root[4]:
                raise RuntimeError("准入游标缺少一致的 acceptance/root")
            kind, subject, key = CommitKind.ACCEPTANCE.value, admission.turn_id, strict_text(
                root[1], field="turn_acceptances.acceptance_idempotency_key",
            )
            metadata = {"accepted_ingress_id": root[0], "turn_id": admission.turn_id}
            item_ids = ((root[3], root[4], root[5]),)
            mode = "item_bearing"
        else:
            kind, subject, key = CommitKind.EXECUTION_ADMISSION.value, admission.execution_id, f"internal-admission:{admission.admission_idempotency_key}"
            rows = connection.execute(
                "SELECT metadata_json FROM storage_commits WHERE commit_kind=? AND subject_id=? AND idempotency_key=?",
                (kind, subject, key),
            ).fetchall()
            if len(rows) != 1:
                raise RuntimeError(f"internal 准入游标缺少唯一提交: execution_id={admission.execution_id}")
            metadata = json.loads(strict_text(rows[0][0], field="storage_commits.metadata_json"))
            if not isinstance(metadata, dict):
                raise RuntimeError("internal 准入提交 metadata 必须是对象")
            request_input = metadata.get("request_input")
            if not isinstance(request_input, dict):
                raise RuntimeError("internal 准入提交缺少冻结 request input")
            expected_hash = connection.execute(
                "SELECT admission_payload_hash FROM executions WHERE execution_id=?",
                (admission.execution_id,),
            ).fetchone()[0]
            self.internal_execution_admission_preimage(
                connection, session_id=admission.session_id, thread_id=admission.thread_id,
                checkpoint_ns=checkpoint_ns, execution_id=admission.execution_id,
                job_id=admission.job_id, admission_key=admission.admission_idempotency_key,
                admission_hash=expected_hash,
            )
            notice_id = strict_optional_text(metadata.get("notice_item_id"), field="internal.notice_item_id")
            is_notice = request_input["kind"] == "runtime_notice"
            if is_notice != (notice_id is not None) or metadata != {
                "execution_id": admission.execution_id, "job_id": admission.job_id,
                "notice_item_id": notice_id, "request_input": request_input,
            }:
                raise RuntimeError("internal 准入提交与 execution/notice identity 不一致")
            mode = "item_bearing" if is_notice else "metadata_only"
            item_ids = ()
            if notice_id is not None:
                notice = connection.execute(
                    "SELECT item_id,content_hash,item_sequence FROM item_catalog "
                    "WHERE item_id=? AND execution_id=? AND semantic_kind='runtime_notice' "
                    "AND turn_scope='pending_next_turn' AND turn_id IS NULL",
                    (notice_id, admission.execution_id),
                ).fetchone()
                if notice is None:
                    raise RuntimeError("internal 准入游标缺少同 execution 的 notice")
                storage_session, storage_thread = current_scope(connection)
                item = self._storage._read_canonical_item_from_catalog(
                    connection, session_id=storage_session, thread_id=storage_thread,
                    checkpoint_ns=checkpoint_ns, item_id=notice_id,
                )
                if request_input != {"kind": "runtime_notice", "payload": item.payload}:
                    raise RuntimeError("internal 准入游标与本地冻结 notice payload 不一致")
                item_ids = (tuple(notice),)
        rows = connection.execute(
            "SELECT commit_id FROM storage_commits WHERE commit_kind=? AND subject_id=? AND idempotency_key=?",
            (kind, subject, key),
        ).fetchall()
        if len(rows) != 1:
            raise RuntimeError(f"准入游标缺少唯一 typed commit: execution_id={admission.execution_id}")
        commit = load_committed_idempotency_commit(
            connection, commit_kind=kind, subject_id=subject, idempotency_key=key,
            commit_mode=mode, outcome=None, metadata=metadata, item_count=len(item_ids),
        )
        if commit is None or read_committed_item_identities(connection, commit_id=commit.commit_id) != item_ids:
            raise RuntimeError("准入游标提交与 root/notice item binding 不一致")
        return ExecutionAdmissionCursor(admission, commit.commit_id, kind)

    def execution_context_excluded_item_ids(
        self, admission: ExecutionAdmission, *, connection: sqlite3.Connection,
        checkpoint_ns: str = "",
    ) -> frozenset[str]:
        """只排除未来准入 root/notice 及所属执行，当前执行的晚到输出继续可见。"""
        cursor = self.execution_admission_cursor(admission, connection=connection, checkpoint_ns=checkpoint_ns)
        rows = connection.execute(
            "SELECT execution.execution_id FROM executions AS execution "
            "LEFT JOIN turn_records AS turn ON turn.turn_id=execution.turn_id "
            "AND turn.session_id=execution.session_id AND turn.thread_id=execution.thread_id "
            "WHERE execution.session_id=? AND execution.thread_id=? "
            "AND (execution.execution_origin='internal' OR execution.execution_origin='user_turn' "
            "AND (execution.execution_id=turn.initial_execution_id OR turn.turn_id IS NULL))",
            (admission.session_id, admission.thread_id),
        ).fetchall()
        excluded: set[str] = set()
        for row in rows:
            future = self._read_execution_admission_connection(
                connection, session_id=admission.session_id, thread_id=admission.thread_id,
                execution_id=strict_text(row[0], field="executions.execution_id"),
                checkpoint_ns=checkpoint_ns,
            )
            future_cursor = self.execution_admission_cursor(future, connection=connection, checkpoint_ns=checkpoint_ns)
            if future_cursor.commit_id <= cursor.commit_id:
                continue
            items = connection.execute(
                "SELECT item_id FROM item_catalog WHERE execution_id=? "
                "OR (? IS NOT NULL AND turn_id=?)",
                (future.execution_id, future.turn_id, future.turn_id),
            ).fetchall()
            excluded.update(strict_text(item[0], field="item_catalog.item_id") for item in items)
        return frozenset(excluded)

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
    ) -> ExecutionAdmission:
        """原子接受唯一 canonical request；seed/reference 不产生 notice。"""
        owner_thread_id = self._storage._resolve_thread_identity(session_id, thread_id)
        with self._execution_input_transaction(session_id, owner_thread_id, checkpoint_ns) as connection:
            return self._admit_internal_execution_transaction(
                connection, session_id, thread_id=owner_thread_id,
                execution_id=execution_id, job_id=job_id,
                admission_idempotency_key=admission_idempotency_key,
                runtime_notice_item=runtime_notice_item,
                request_input=request_input, checkpoint_ns=checkpoint_ns,
            )

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
        owner_thread_id = self._storage._resolve_thread_identity(session_id, thread_id)
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
            and set(notice_item.payload) == {"prompt", "display_part"}
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

    def accepted_user_input(
        self, admission: ExecutionAdmission, *, checkpoint_ns: str = "",
    ) -> CanonicalItemRecord:
        """只读取 acceptance 已冻结的真实用户根输入，不创建或补造任何事实。"""
        if admission.execution_origin != "user_turn" or admission.turn_id is None:
            raise ValueError("读取用户根输入必须提供真实 user-turn admission")
        with self._storage.read_connection(
            admission.session_id, admission.thread_id, checkpoint_ns,
        ) as connection:
            stored = self.execution_admission(
                admission.session_id, thread_id=admission.thread_id,
                execution_id=admission.execution_id, checkpoint_ns=checkpoint_ns,
                writer_connection=connection,
            )
            if stored != admission:
                raise ValueError("用户根输入 admission 与权威执行身份不一致")
            root = connection.execute(
                "SELECT turn.root_input_item_id, acceptance.payload_hash "
                "FROM turn_records AS turn JOIN turn_acceptances AS acceptance "
                "ON acceptance.session_id=turn.session_id AND acceptance.thread_id=turn.thread_id "
                "AND acceptance.turn_id=turn.turn_id WHERE turn.session_id=? AND turn.thread_id=? AND turn.turn_id=?",
                (admission.session_id, admission.thread_id, admission.turn_id),
            ).fetchone()
            if root is None:
                raise RuntimeError("用户执行缺少同 owner 的 acceptance/root")
            item = self._storage.read_items(
                admission.session_id, admission.thread_id, checkpoint_ns=checkpoint_ns,
                item_ids=(strict_text(root[0], field="turn_records.root_input_item_id"),),
                connection=connection,
            )[0]
            if (item.semantic_kind != SemanticKind.USER_INPUT or item.turn_scope != TurnScope.TURN_ROOT
                    or item.turn_id != admission.turn_id or item.content_hash != root[1]):
                raise RuntimeError("已接受用户根输入与 Turn/acceptance 不一致")
            return item

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
        owner_thread_id = self._storage._resolve_thread_identity(session_id, thread_id)
        job_id = strict_text(job_id, field="job_id")
        checkpoint_ns = strict_text(
            checkpoint_ns, field="checkpoint_ns", allow_empty=True
        )
        owner = (
            f"session_id={session_id!r}, thread_id={owner_thread_id!r}, "
            f"job_id={job_id!r}, checkpoint_ns={checkpoint_ns!r}"
        )
        try:
            with self._storage.read_connection(session_id, owner_thread_id, checkpoint_ns) as connection:
                self._storage._require_v2_runtime(connection)
                rows = connection.execute(
                    "SELECT execution_id FROM executions "
                    "WHERE session_id = ? AND thread_id = ? AND job_id = ? "
                    "ORDER BY execution_id LIMIT 2",
                    (session_id, owner_thread_id, job_id),
                ).fetchall()
        except RolloutNotMaterializedError as error:
            raise KeyError(f"Job execution admission 不存在: {owner}") from error
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
        writer_connection: sqlite3.Connection | None = None,
    ) -> ExecutionAdmission:
        """读取并核验 owner 下的 user-turn/internal execution admission。"""
        session_id = strict_text(session_id, field="session_id")
        owner_thread_id = self._storage._resolve_thread_identity(session_id, thread_id)
        execution_id = strict_text(execution_id, field="execution_id")
        checkpoint_ns = strict_text(
            checkpoint_ns,
            field="checkpoint_ns",
            allow_empty=True,
        )
        with self._storage.read_connection(session_id, owner_thread_id, checkpoint_ns, connection=writer_connection) as connection:
            return self._read_execution_admission_connection(
                connection, session_id=session_id, thread_id=owner_thread_id,
                execution_id=execution_id, checkpoint_ns=checkpoint_ns,
            )

    def _read_execution_admission_connection(
        self, connection: sqlite3.Connection, *, session_id: str, thread_id: str,
        execution_id: str, checkpoint_ns: str = "",
    ) -> ExecutionAdmission:
        """同一只读 loader 核当前或本地冻结历史 scope；不借历史身份取得 writer。"""
        require_historical_scope(connection, session_id, thread_id)
        self._storage._require_v2_runtime(connection)
        row = connection.execute(
            "SELECT turn_id, execution_origin, job_id, "
            "admission_idempotency_key, admission_payload_hash, accepted_ingress_id "
            "FROM executions WHERE session_id = ? AND thread_id = ? AND execution_id = ?",
            (session_id, thread_id, execution_id),
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
                (session_id, thread_id, turn_id),
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
                thread_id=thread_id,
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
            thread_id=thread_id,
            execution_id=execution_id,
            job_id=job_id,
            admission_idempotency_key=admission_key,
            execution_origin=origin,
            turn_id=turn_id,
        )


__all__ = ["RolloutExecutionAdmissionMixin"]
