"""Rollout v2 Turn/execution/model-call 持久化 owner。

这些 mixin 只依赖 RolloutContextStore 提供的 SQLite、JSONL transaction 和 domain
ports；它们不承担 LangChain/provider projection，也不读取 v1 数据。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from uuid import uuid4

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.enums import (
    CanonicalItemStatus,
    CommitKind,
    PayloadKind,
    SemanticKind,
    TurnScope,
    TurnStatus,
)
from app.domain.itemized.execution_admission import user_turn_admission_payload_hash
from app.domain.itemized.hashing import content_hash as item_content_hash
from app.domain.itemized.identity.item_projection import (
    ItemProjectionIdentity,
    ItemProvenanceRelation,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.schema import validate_extensions
from app.services.infrastructure.rollout_context.execution.runtime_notice_consumption import (
    acceptance_metadata,
    freeze_runtime_notice_selection,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
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


class RolloutTurnsMixin:
    """Acceptance-time Turn/root 与显式 replay-as-new-turn owner。"""

    def accept_turn(
        self,
        session_id: str,
        *,
        thread_id: str | None = None,
        accepted_ingress_id: str,
        acceptance_idempotency_key: str,
        job_id: str,
        admission_idempotency_key: str,
        payload: object,
        payload_kind: str = PayloadKind.TEXT,
        checkpoint_ns: str = "",
        branch_id: str | None = None,
        turn_id: str | None = None,
        turn_ordinal: int | None = None,
        root_item_id: str | None = None,
        initial_execution_id: str | None = None,
        extensions: Mapping[str, object] | None = None,
        source_session_id: str | None = None,
        source_accepted_ingress_id: str | None = None,
        source_acceptance_idempotency_key: str | None = None,
        provenance_relations: tuple[ItemProvenanceRelation, ...] = (),
        replay_of_turn_id: str | None = None,
        identity_origin: str = "runtime",
    ) -> dict[str, object]:
        """在同一事务建立 acceptance、Turn、root item 与 initial execution。"""
        session_id = strict_text(session_id, field="session_id")
        owner_thread_id = self._storage._resolve_thread_identity(session_id, thread_id)
        strict_text(accepted_ingress_id, field="accepted_ingress_id")
        strict_text(
            acceptance_idempotency_key,
            field="acceptance_idempotency_key",
        )
        job_id = strict_text(job_id, field="job_id")
        admission_idempotency_key = strict_text(
            admission_idempotency_key,
            field="admission_idempotency_key",
        )
        payload_kind = strict_text(payload_kind, field="payload_kind")
        extensions_value = dict(extensions) if extensions is not None else {}
        validate_extensions(extensions_value)
        source_session_id = strict_optional_text(
            source_session_id, field="source_session_id"
        )
        source_accepted_ingress_id = strict_optional_text(
            source_accepted_ingress_id, field="source_accepted_ingress_id"
        )
        source_acceptance_idempotency_key = strict_optional_text(
            source_acceptance_idempotency_key, field="source_acceptance_idempotency_key"
        )
        strict_text(checkpoint_ns, field="checkpoint_ns", allow_empty=True)
        branch_id = strict_optional_text(branch_id, field="branch_id")
        turn_id = strict_optional_text(turn_id, field="turn_id")
        root_item_id = strict_optional_text(root_item_id, field="root_item_id")
        initial_execution_id = strict_optional_text(
            initial_execution_id,
            field="initial_execution_id",
        )
        replay_of_turn_id = strict_optional_text(
            replay_of_turn_id,
            field="replay_of_turn_id",
        )
        strict_text(identity_origin, field="identity_origin")
        with self._storage._lock(owner_thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(session_id, checkpoint_ns, thread_id=owner_thread_id)
            with self._storage._connect(
                owner_thread_id, checkpoint_ns, session_id=session_id
            ) as connection:
                self._storage._require_v2_runtime(connection)
                connection.execute("BEGIN IMMEDIATE")
                if branch_id is None:
                    active_branch_row = connection.execute(
                        "SELECT active_branch_id FROM checkpoint_namespace_state WHERE checkpoint_ns = ?",
                        (checkpoint_ns,),
                    ).fetchone()
                    branch_value = (
                        strict_text(active_branch_row[0], field="active_branch_id")
                        if active_branch_row is not None and active_branch_row[0]
                        else None
                    )
                    if branch_value is None:
                        raise RuntimeError("acceptance 缺少 active source branch")
                else:
                    branch_value = branch_id
                if branch_value is None:
                    raise RuntimeError("acceptance 缺少 active source branch")
                payload_digest = item_content_hash(payload_kind, payload)
                existing = connection.execute(
                    "SELECT ta.accepted_ingress_id, ta.acceptance_idempotency_key, "
                    "ta.turn_id, ta.payload_hash, tr.source_branch_id, "
                    "tr.initial_execution_id, e.job_id, e.admission_idempotency_key, "
                    "e.admission_payload_hash "
                    "FROM turn_acceptances AS ta LEFT JOIN turn_records AS tr "
                    "ON tr.session_id = ta.session_id AND tr.thread_id = ta.thread_id "
                    "AND tr.turn_id = ta.turn_id "
                    "LEFT JOIN executions AS e ON e.session_id = tr.session_id "
                    "AND e.thread_id = tr.thread_id "
                    "AND e.execution_id = tr.initial_execution_id "
                    "WHERE ta.session_id = ? AND ta.thread_id = ? "
                    "AND (ta.accepted_ingress_id = ? OR ta.acceptance_idempotency_key = ?)",
                    (
                        session_id,
                        owner_thread_id,
                        accepted_ingress_id,
                        acceptance_idempotency_key,
                    ),
                ).fetchall()
                if existing:
                    if len(existing) > 1:
                        raise RuntimeError(
                            "acceptance identity 索引损坏：一个 ingress/key 命中多个记录"
                        )
                    row = existing[0]
                    existing_ingress_id = strict_text(
                        row[0], field="turn_acceptances.accepted_ingress_id"
                    )
                    existing_acceptance_key = strict_text(
                        row[1], field="turn_acceptances.acceptance_idempotency_key"
                    )
                    existing_turn_id = strict_text(
                        row[2], field="turn_acceptances.turn_id"
                    )
                    existing_payload_hash = strict_text(
                        row[3], field="turn_acceptances.payload_hash"
                    )
                    existing_branch_id = strict_text(
                        row[4], field="turn_records.source_branch_id"
                    )
                    if existing_ingress_id != accepted_ingress_id:
                        raise ValueError(
                            "acceptance_idempotency_key 冲突：同 key 不能绑定不同 accepted_ingress_id"
                        )
                    if existing_acceptance_key != acceptance_idempotency_key:
                        raise ValueError(
                            "accepted_ingress_id 冲突：同 ingress 不能绑定不同 acceptance_idempotency_key"
                        )
                    if existing_payload_hash != payload_digest:
                        raise ValueError(
                            "acceptance identity 冲突：同 key/ingress 对应不同 payload"
                        )
                    if existing_branch_id != branch_value:
                        raise ValueError(
                            "acceptance identity 冲突：同 ingress/key 不能绑定不同 source branch"
                        )
                    existing_execution = strict_text(
                        row[5], field="turn_records.initial_execution_id"
                    )
                    existing_job_id = strict_text(row[6], field="executions.job_id")
                    existing_admission_key = strict_text(
                        row[7], field="executions.admission_idempotency_key"
                    )
                    existing_admission_hash = strict_text(
                        row[8], field="executions.admission_payload_hash"
                    )
                    expected_admission_hash = user_turn_admission_payload_hash(
                        session_id=session_id,
                        thread_id=owner_thread_id,
                        turn_id=existing_turn_id,
                        execution_id=existing_execution,
                        job_id=job_id,
                        accepted_ingress_id=accepted_ingress_id,
                        admission_idempotency_key=admission_idempotency_key,
                        payload_hash=payload_digest,
                    )
                    if existing_admission_hash != expected_admission_hash:
                        raise RuntimeError(
                            "existing user_turn execution admission payload hash 不匹配"
                        )
                    if existing_job_id != job_id:
                        raise ValueError("acceptance identity 已绑定不同 job_id")
                    if existing_admission_key != admission_idempotency_key:
                        raise ValueError("acceptance identity 已绑定不同 admission key")
                    existing_turn = connection.execute(
                        "SELECT turn_id, root_input_item_id, initial_execution_id, status, final_item_id "
                        "FROM turn_records WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
                        (session_id, owner_thread_id, existing_turn_id),
                    ).fetchone()
                    if existing_turn is None:
                        raise RuntimeError(
                            "acceptance 已存在但 Turn 缺失，拒绝静默补造"
                        )
                    stored_turn_id = strict_text(
                        existing_turn[0], field="turn_records.turn_id"
                    )
                    stored_root_item_id = strict_text(
                        existing_turn[1], field="turn_records.root_input_item_id"
                    )
                    stored_execution_id = strict_text(
                        existing_turn[2], field="turn_records.initial_execution_id"
                    )
                    stored_status = strict_text(
                        existing_turn[3], field="turn_records.status"
                    )
                    stored_final_item_id = strict_optional_text(
                        existing_turn[4], field="turn_records.final_item_id"
                    )
                    root_execution = connection.execute(
                        "SELECT execution_id FROM item_catalog WHERE item_id = ?",
                        (stored_root_item_id,),
                    ).fetchone()
                    if (
                        root_execution is None
                        or strict_text(
                            root_execution[0],
                            field="item_catalog.execution_id:acceptance_root",
                        )
                        != stored_execution_id
                    ):
                        raise RuntimeError(
                            "existing acceptance root 未绑定 initial execution: "
                            f"turn_id={stored_turn_id}, execution_id={stored_execution_id}"
                        )
                    if turn_id is not None and stored_turn_id != turn_id:
                        raise ValueError("acceptance identity 冲突：turn_id 不一致")
                    if root_item_id is not None and stored_root_item_id != root_item_id:
                        raise ValueError(
                            "acceptance identity 冲突：root_item_id 不一致"
                        )
                    if (
                        initial_execution_id is not None
                        and stored_execution_id != initial_execution_id
                    ):
                        raise ValueError(
                            "acceptance identity 冲突：initial_execution_id 不一致"
                        )
                    execution = connection.execute(
                        "SELECT execution_id FROM executions WHERE session_id = ? AND thread_id = ? AND execution_id = ? AND turn_id = ?",
                        (
                            session_id,
                            owner_thread_id,
                            stored_execution_id,
                            stored_turn_id,
                        ),
                    ).fetchone()
                    if execution is None:
                        raise RuntimeError("acceptance 已存在但 initial execution 缺失")
                    with self._storage.jsonl_path(
                        session_id, checkpoint_ns, thread_id=owner_thread_id
                    ).open("rb") as root_stream:
                        root_locator = connection.execute(
                            "SELECT item_sequence, jsonl_offset, jsonl_length FROM item_catalog WHERE item_id = ?",
                            (stored_root_item_id,),
                        ).fetchone()
                        restored_root = self._storage._read_v2_item_at(
                            connection,
                            root_stream,
                            sequence=strict_non_negative_int(
                                root_locator[0], field="root.item_sequence"
                            ),
                            offset=strict_non_negative_int(
                                root_locator[1], field="root.jsonl_offset"
                            ),
                            length=strict_non_negative_int(
                                root_locator[2], field="root.jsonl_length"
                            ),
                        )
                    if _json(restored_root.extensions) != _json(extensions_value):
                        raise ValueError("acceptance identity 冲突：extensions 不一致")
                    if restored_root.provenance_relations != provenance_relations:
                        raise ValueError(
                            "acceptance identity 冲突：typed provenance relations 不一致"
                        )
                    accepted_commit = acceptance_metadata(
                        connection, turn_id=stored_turn_id,
                        ingress_id=accepted_ingress_id, acceptance_key=acceptance_idempotency_key,
                    )
                    if accepted_commit is None:
                        identity_origin_row = connection.execute(
                            "SELECT identity_origin FROM turn_acceptances WHERE session_id = ? AND thread_id = ? AND accepted_ingress_id = ?",
                            (session_id, owner_thread_id, accepted_ingress_id),
                        ).fetchone()
                        if (
                            identity_origin is None
                            or strict_text(
                                identity_origin_row[0],
                                field="turn_acceptances.identity_origin",
                            )
                            != "checkpoint_origin"
                        ):
                            raise RuntimeError(
                                "acceptance 已存在但 acceptance commit 缺失"
                            )
                        checkpoint_commit = connection.execute(
                            "SELECT ic.commit_id, ic.content_hash "
                            "FROM item_catalog AS ic "
                            "JOIN turn_records AS tr ON tr.root_input_item_id = ic.item_id "
                            "WHERE tr.turn_id = ?",
                            (stored_turn_id,),
                        ).fetchone()
                        if (
                            checkpoint_commit is None
                            or checkpoint_commit[0] is None
                            or checkpoint_commit[1] != existing_payload_hash
                        ):
                            raise RuntimeError(
                                "checkpoint-origin acceptance 缺少一致的 root item commit"
                            )
                        if (
                            not isinstance(checkpoint_commit[0], int)
                            or isinstance(checkpoint_commit[0], bool)
                            or not isinstance(checkpoint_commit[1], str)
                        ):
                            raise RuntimeError(
                                "checkpoint-origin acceptance root item commit 字段类型非法"
                            )
                        commit_id = checkpoint_commit[0]
                    else:
                        commit_id = accepted_commit[1]
                    return {
                        "turn_id": stored_turn_id,
                        "root_input_item_id": stored_root_item_id,
                        "initial_execution_id": stored_execution_id,
                        "execution_id": stored_execution_id,
                        "job_id": existing_job_id,
                        "admission_idempotency_key": existing_admission_key,
                        "admission_payload_hash": existing_admission_hash,
                        "status": stored_status,
                        "final_item_id": stored_final_item_id,
                        "commit_id": commit_id,
                        "idempotent": True,
                    }
                turn_value = (
                    turn_id
                    or "turn-"
                    + hashlib.sha256(
                        (
                            session_id
                            + ":"
                            + owner_thread_id
                            + ":"
                            + accepted_ingress_id
                        ).encode("utf-8")
                    ).hexdigest()[:24]
                )
                turn_value = strict_text(turn_value, field="turn_id")
                branch_exists = connection.execute(
                    "SELECT 1 FROM branches WHERE branch_id = ? AND status = 'active'",
                    (branch_value,),
                ).fetchone()
                if branch_exists is None:
                    raise ValueError(f"acceptance source branch 不可用: {branch_value}")
                # 导入历史保留原 owner，仍占用当前 Thread 数据库的序号空间。
                ordinal = (
                    turn_ordinal
                    if turn_ordinal is not None
                    else strict_non_negative_int(
                        connection.execute(
                            "SELECT COALESCE(MAX(turn_ordinal), 0) + 1 FROM turn_records",
                        ).fetchone()[0],
                        field="turn_records.turn_ordinal",
                    )
                )
                if (
                    not isinstance(ordinal, int)
                    or isinstance(ordinal, bool)
                    or ordinal <= 0
                ):
                    raise ValueError("turn_ordinal 必须是正整数")
                if (
                    connection.execute(
                        "SELECT 1 FROM turn_records WHERE turn_ordinal = ?",
                        (ordinal,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError("turn_ordinal 已存在，禁止重排 Turn")
                if (
                    connection.execute(
                        "SELECT 1 FROM turn_records WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
                        (session_id, owner_thread_id, turn_value),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError("turn_id 已存在但 acceptance identity 不存在")
                execution_id = initial_execution_id or "execution-" + uuid4().hex
                execution_id = strict_text(
                    execution_id,
                    field="initial_execution_id",
                )
                if (
                    connection.execute(
                        "SELECT turn_id FROM executions WHERE session_id = ? AND thread_id = ? AND execution_id = ?",
                        (session_id, owner_thread_id, execution_id),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        "initial_execution_id 已被其它 Turn 使用，拒绝复用"
                    )
                admission_job_id = job_id
                admission_key = admission_idempotency_key
                admission_hash = user_turn_admission_payload_hash(
                    session_id=session_id,
                    thread_id=owner_thread_id,
                    turn_id=turn_value,
                    execution_id=execution_id,
                    job_id=admission_job_id,
                    accepted_ingress_id=accepted_ingress_id,
                    admission_idempotency_key=admission_key,
                    payload_hash=payload_digest,
                )
                item_sequence_row = connection.execute(
                    "SELECT last_item_sequence FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if item_sequence_row is None:
                    raise RuntimeError(
                        "acceptance 缺少 database_meta.last_item_sequence"
                    )
                item_sequence = (
                    strict_non_negative_int(
                        item_sequence_row[0],
                        field="database_meta.last_item_sequence",
                    )
                    + 1
                )
                root_item_value = root_item_id or "item-root-" + uuid4().hex
                root = CanonicalItemRecord.create(
                    item_sequence=item_sequence,
                    item_id=root_item_value,
                    semantic_kind=SemanticKind.USER_INPUT,
                    payload_kind=payload_kind,
                    status=CanonicalItemStatus.COMPLETED,
                    producer_ref={
                        "producer_kind": "user",
                        "producer_id": accepted_ingress_id,
                        "invocation_id": execution_id,
                    },
                    payload=payload,
                    provenance_relations=provenance_relations,
                    extensions=extensions_value,
                    projection_identity=ItemProjectionIdentity(
                        projection_message_id=root_item_value.removeprefix("item-"),
                    ),
                    turn_id=turn_value,
                    turn_scope=TurnScope.TURN_ROOT,
                    wire_role="user",
                )
                notice_selection = freeze_runtime_notice_selection(
                    connection, storage=self._storage, session_id=session_id,
                    thread_id=owner_thread_id, checkpoint_ns=checkpoint_ns,
                    branch_id=branch_value, execution_id=execution_id,
                    admission_key=admission_key,
                )
                commit_id, _offset = self._storage._append_v2_records_transaction(
                    connection,
                    owner_thread_id,
                    checkpoint_ns,
                    (root,),
                    session_id=session_id,
                    commit_kind=CommitKind.ACCEPTANCE.value,
                    subject_id=turn_value,
                    idempotency_key=acceptance_idempotency_key,
                    begin_transaction=False,
                    metadata={
                        "accepted_ingress_id": accepted_ingress_id,
                        "turn_id": turn_value,
                        "runtime_notice_selection": notice_selection,
                    },
                )
                self._storage._bind_canonical_item_execution(
                    connection,
                    item_id=root.item_id,
                    execution_id=execution_id,
                )
                message_meta = connection.execute(
                    "SELECT last_message_sequence FROM database_meta "
                    "WHERE singleton_id = 1"
                ).fetchone()
                if message_meta is None:
                    raise RuntimeError("acceptance 缺少 message sequence 元数据")
                previous_last_message_sequence = strict_non_negative_int(
                    message_meta[0],
                    field="database_meta.last_message_sequence",
                )
                root_message_sequence = previous_last_message_sequence + 1
                materialized = self._context.checkpoints._materialize_canonical_message_projection(
                    connection,
                    root,
                    thread_id=owner_thread_id,
                    session_id=session_id,
                    checkpoint_ns=checkpoint_ns,
                    commit_id=commit_id,
                    message_sequence=root_message_sequence,
                    timestamp=_now(),
                )
                if materialized is None:
                    raise RuntimeError("acceptance user root 未生成 message projection")
                (
                    materialized_sequence,
                    projection_message_id,
                    projection_role,
                ) = materialized
                if materialized_sequence != root_message_sequence:
                    raise RuntimeError("acceptance user root message sequence 冲突")
                self._storage._upsert_turn(
                    connection,
                    turn_value,
                    root_message_sequence,
                    projection_message_id,
                    projection_role,
                    branch_value,
                    _now(),
                )
                commit_update = connection.execute(
                    "UPDATE storage_commits SET first_message_sequence = ?, "
                    "last_message_sequence = ? WHERE commit_id = ?",
                    (root_message_sequence, root_message_sequence, commit_id),
                )
                if commit_update.rowcount != 1:
                    raise RuntimeError(
                        "acceptance storage commit message sequence 更新失败"
                    )
                meta_update = connection.execute(
                    "UPDATE database_meta SET last_message_sequence = ?, "
                    "history_view_revision = history_view_revision + 1, "
                    "updated_at = ? WHERE singleton_id = 1",
                    (root_message_sequence, _now()),
                )
                if meta_update.rowcount != 1:
                    raise RuntimeError("acceptance last_message_sequence 更新失败")
                # acceptance 可能发生在首个 LangGraph checkpoint 之前。此时
                # active branch 仍指向初始化 view，若只等待 checkpoint 创建
                # view，首个 provider assembly 会看不到已经提交的 Turn root。
                # root 是 canonical item 的 active-view 成员；在同一 SQLite
                # 事务内登记索引，后续 checkpoint 仍会创建正常的子 view。
                active_view = connection.execute(
                    "SELECT head_view_id FROM branches WHERE branch_id = ? AND status = 'active'",
                    (branch_value,),
                ).fetchone()
                if active_view is not None and active_view[0] is not None:
                    self._storage._append_context_view_items(
                        connection,
                        checkpoint_ns=checkpoint_ns,
                        item_ids=(root.item_id,),
                        branch_id=branch_value,
                    )
                timestamp = _now()
                connection.execute(
                    "INSERT INTO turn_acceptances(accepted_ingress_id, acceptance_idempotency_key, session_id, thread_id, turn_id, payload_hash, source_session_id, source_accepted_ingress_id, source_acceptance_idempotency_key, identity_origin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        accepted_ingress_id,
                        acceptance_idempotency_key,
                        session_id,
                        owner_thread_id,
                        turn_value,
                        payload_digest,
                        source_session_id,
                        source_accepted_ingress_id,
                        source_acceptance_idempotency_key,
                        identity_origin,
                        timestamp,
                    ),
                )
                connection.execute(
                    "INSERT INTO turn_records(turn_id, session_id, thread_id, turn_ordinal, source_branch_id, root_input_item_id, root_input_item_sequence, accepted_ingress_id, acceptance_idempotency_key, initial_execution_id, last_execution_id, status, replay_of_turn_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                    (
                        turn_value,
                        session_id,
                        owner_thread_id,
                        ordinal,
                        branch_value,
                        root.item_id,
                        root.item_sequence,
                        accepted_ingress_id,
                        acceptance_idempotency_key,
                        execution_id,
                        execution_id,
                        replay_of_turn_id,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "UPDATE turns SET status='accepted' WHERE turn_id=?", (turn_value,),
                )
                # acceptance-time 可能早于第一个 LangGraph checkpoint。除了
                # canonical item membership，还必须立即登记 active view 的
                # Turn logical ordinal；否则 replay_as_new_turn/新输入虽然
                # 已经有 root，却会在即时 history/root lookup 中暂时消失，
                # 直到下一次 checkpoint 偶然重建派生索引。
                view_id = self._storage._active_context_view_id(connection, checkpoint_ns, branch_value)
                if view_id is not None:
                    existing_view_turn = connection.execute(
                        "SELECT logical_turn_ordinal, root_input_item_id FROM context_view_turns WHERE view_id = ? AND turn_id = ?",
                        (view_id, turn_value),
                    ).fetchone()
                    if existing_view_turn is None:
                        next_turn_ordinal = strict_non_negative_int(
                            connection.execute(
                                "SELECT COALESCE(MAX(logical_turn_ordinal), 0) + 1 FROM context_view_turns WHERE view_id = ?",
                                (view_id,),
                            ).fetchone()[0],
                            field="context_view_turns.logical_turn_ordinal",
                        )
                        connection.execute(
                            "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence, root_input_item_id, fork_lineage_json) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                            (
                                view_id,
                                turn_value,
                                next_turn_ordinal,
                                root_message_sequence,
                                root.item_id,
                                _json({"source": "acceptance"}),
                            ),
                        )
                        connection.execute(
                            "UPDATE context_views SET head_turn_id = ?, logical_turn_count = MAX(logical_turn_count, ?) WHERE view_id = ?",
                            (turn_value, next_turn_ordinal, view_id),
                        )
                    elif (
                        strict_text(
                            existing_view_turn[1],
                            field="context_view_turns.root_input_item_id",
                        )
                        != root.item_id
                    ):
                        raise RuntimeError(
                            f"active view Turn root identity 冲突: turn_id={turn_value}"
                        )
                connection.execute(
                    "INSERT INTO executions(execution_id, session_id, thread_id, turn_id, execution_origin, job_id, admission_idempotency_key, admission_payload_hash, attempt, execution_ordinal, accepted_ingress_id, outcome, created_at, updated_at) VALUES (?, ?, ?, ?, 'user_turn', ?, ?, ?, 1, 1, ?, 'unknown', ?, ?)",
                    (
                        execution_id,
                        session_id,
                        owner_thread_id,
                        turn_value,
                        admission_job_id,
                        admission_key,
                        admission_hash,
                        accepted_ingress_id,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "INSERT INTO turn_execution_links(session_id, thread_id, turn_id, execution_id, execution_role, execution_ordinal, link_idempotency_key, created_at) VALUES (?, ?, ?, ?, 'initial', 1, ?, ?)",
                    (
                        session_id,
                        owner_thread_id,
                        turn_value,
                        execution_id,
                        f"{turn_value}:initial:{execution_id}",
                        timestamp,
                    ),
                )
                self._storage._commit_connection(connection)
                return {
                    "turn_id": turn_value,
                    "root_input_item_id": root.item_id,
                    "initial_execution_id": execution_id,
                    "execution_id": execution_id,
                    "job_id": admission_job_id,
                    "admission_idempotency_key": admission_key,
                    "admission_payload_hash": admission_hash,
                    "commit_id": commit_id,
                    "idempotent": False,
                }
