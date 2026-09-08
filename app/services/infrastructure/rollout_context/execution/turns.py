"""Rollout v2 Turn/execution/model-call 持久化 owner。

这些 mixin 只依赖 RolloutStorage 提供的 SQLite、JSONL transaction 和 domain
ports；它们不承担 LangChain/provider projection，也不读取 v1 数据。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from app.domain.itemized.enums import (
    CanonicalItemStatus,
    CommitKind,
    PayloadKind,
    SemanticKind,
    TurnScope,
    TurnStatus,
)
from app.domain.itemized.hashing import content_hash as item_content_hash
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text as _json,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    load_committed_idempotency_commit,
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


class RolloutTurnsMixin:
    """Acceptance-time Turn/root 与显式 replay-as-new-turn owner。"""

    def accept_turn(
        self,
        thread_id: str,
        *,
        accepted_ingress_id: str,
        acceptance_idempotency_key: str,
        payload: object,
        payload_kind: str = PayloadKind.TEXT,
        checkpoint_ns: str = "",
        branch_id: str | None = None,
        turn_id: str | None = None,
        turn_ordinal: int | None = None,
        root_item_id: str | None = None,
        initial_execution_id: str | None = None,
        acceptance_metadata: Mapping[str, object] | None = None,
        replay_of_turn_id: str | None = None,
        identity_origin: str = "runtime",
    ) -> dict[str, object]:
        """在同一事务建立 acceptance、Turn、root item 与 initial execution。"""
        strict_text(thread_id, field="session_id")
        strict_text(accepted_ingress_id, field="accepted_ingress_id")
        strict_text(
            acceptance_idempotency_key,
            field="acceptance_idempotency_key",
        )
        payload_kind = strict_text(payload_kind, field="payload_kind")
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
        with self._lock(thread_id, checkpoint_ns):
            self.initialize(thread_id, checkpoint_ns)
            with self._connect(thread_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
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
                    "ta.turn_id, ta.payload_hash, tr.source_branch_id "
                    "FROM turn_acceptances AS ta LEFT JOIN turn_records AS tr "
                    "ON tr.turn_id = ta.turn_id "
                    "WHERE ta.accepted_ingress_id = ? OR ta.acceptance_idempotency_key = ?",
                    (accepted_ingress_id, acceptance_idempotency_key),
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
                    existing_turn = connection.execute(
                        "SELECT turn_id, root_input_item_id, initial_execution_id, status, final_item_id FROM turn_records WHERE turn_id = ?",
                        (existing_turn_id,),
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
                    if turn_id is not None and stored_turn_id != turn_id:
                        raise ValueError("acceptance identity 冲突：turn_id 不一致")
                    if (
                        root_item_id is not None
                        and stored_root_item_id != root_item_id
                    ):
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
                        "SELECT execution_id FROM executions WHERE execution_id = ? AND turn_id = ?",
                        (stored_execution_id, stored_turn_id),
                    ).fetchone()
                    if execution is None:
                        raise RuntimeError("acceptance 已存在但 initial execution 缺失")
                    accepted_commit = load_committed_idempotency_commit(
                        connection,
                        commit_kind=CommitKind.ACCEPTANCE.value,
                        subject_id=stored_turn_id,
                        idempotency_key=acceptance_idempotency_key,
                        commit_mode="item_bearing",
                        outcome=None,
                        metadata={
                            "accepted_ingress_id": accepted_ingress_id,
                            "turn_id": stored_turn_id,
                        },
                        item_count=1,
                    )
                    if accepted_commit is None:
                        identity_origin_row = connection.execute(
                            "SELECT identity_origin FROM turn_acceptances WHERE accepted_ingress_id = ?",
                            (accepted_ingress_id,),
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
                        commit_id = accepted_commit.commit_id
                    return {
                        "turn_id": stored_turn_id,
                        "root_input_item_id": stored_root_item_id,
                        "initial_execution_id": stored_execution_id,
                        "status": stored_status,
                        "final_item_id": stored_final_item_id,
                        "commit_id": commit_id,
                        "idempotent": True,
                    }
                turn_value = (
                    turn_id
                    or "turn-"
                    + hashlib.sha256(
                        (thread_id + ":" + accepted_ingress_id).encode("utf-8")
                        ).hexdigest()[:24]
                )
                turn_value = strict_text(turn_value, field="turn_id")
                branch_exists = connection.execute(
                    "SELECT 1 FROM branches WHERE branch_id = ? AND status = 'active'",
                    (branch_value,),
                ).fetchone()
                if branch_exists is None:
                    raise ValueError(f"acceptance source branch 不可用: {branch_value}")
                ordinal = (
                    turn_ordinal
                    if turn_ordinal is not None
                    else strict_non_negative_int(
                        connection.execute(
                            "SELECT COALESCE(MAX(turn_ordinal), 0) + 1 FROM turn_records"
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
                        "SELECT 1 FROM turn_records WHERE turn_id = ?",
                        (turn_value,),
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
                        "SELECT turn_id FROM executions WHERE execution_id = ?",
                        (execution_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError(
                        "initial_execution_id 已被其它 Turn 使用，拒绝复用"
                    )
                item_sequence_row = connection.execute(
                    "SELECT last_item_sequence FROM database_meta WHERE singleton_id = 1"
                ).fetchone()
                if item_sequence_row is None:
                    raise RuntimeError("acceptance 缺少 database_meta.last_item_sequence")
                item_sequence = (
                    strict_non_negative_int(
                        item_sequence_row[0],
                        field="database_meta.last_item_sequence",
                    )
                    + 1
                )
                if acceptance_metadata is not None and not isinstance(
                    acceptance_metadata, Mapping
                ):
                    raise TypeError("acceptance_metadata 必须是 object")
                acceptance_metadata_value = dict(acceptance_metadata or {})
                protected_metadata_keys = {
                    "accepted_ingress_id",
                    "turn_id",
                    "execution_id",
                    "projection_message_id",
                    "turn_scope",
                    "semantic_kind",
                    "payload_kind",
                    "content_hash",
                }
                overridden = sorted(
                    protected_metadata_keys.intersection(acceptance_metadata_value)
                )
                if overridden:
                    raise ValueError(
                        "acceptance metadata 不得覆盖 canonical identity 字段: "
                        + ",".join(overridden)
                    )
                root = CanonicalItemRecord.create(
                    item_sequence=item_sequence,
                    item_id=root_item_id or "item-root-" + uuid4().hex,
                    semantic_kind=SemanticKind.USER_INPUT,
                    payload_kind=payload_kind,
                    status=CanonicalItemStatus.COMPLETED,
                    producer_ref={
                        "producer_kind": "user",
                        "producer_id": accepted_ingress_id,
                        "invocation_id": execution_id,
                    },
                    payload=payload,
                    metadata={
                        **acceptance_metadata_value,
                        "accepted_ingress_id": accepted_ingress_id,
                        "turn_id": turn_value,
                        "execution_id": execution_id,
                        "projection_message_id": (
                            root_item_id[len("item-") :]
                            if isinstance(root_item_id, str)
                            and root_item_id.startswith("item-")
                            else root_item_id
                        ),
                    },
                    turn_id=turn_value,
                    turn_scope=TurnScope.TURN_ROOT,
                    wire_role="user",
                )
                connection.execute("BEGIN IMMEDIATE")
                commit_id, _offset = self._append_v2_records_transaction(
                    connection,
                    thread_id,
                    checkpoint_ns,
                    (root,),
                    commit_kind=CommitKind.ACCEPTANCE.value,
                    subject_id=turn_value,
                    idempotency_key=acceptance_idempotency_key,
                    begin_transaction=False,
                    metadata={
                        "accepted_ingress_id": accepted_ingress_id,
                        "turn_id": turn_value,
                    },
                )
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
                    self._append_context_view_items(
                        connection,
                        checkpoint_ns=checkpoint_ns,
                        item_ids=(root.item_id,),
                        branch_id=branch_value,
                    )
                timestamp = _now()
                acceptance_metadata_value = dict(acceptance_metadata or {})
                source_session_id = acceptance_metadata_value.get(
                    "source_session_id",
                    acceptance_metadata_value.get("legacy_source_session_id"),
                )
                source_ingress_id = acceptance_metadata_value.get(
                    "source_accepted_ingress_id",
                    acceptance_metadata_value.get("legacy_source_accepted_ingress_id"),
                )
                source_acceptance_key = acceptance_metadata_value.get(
                    "source_acceptance_idempotency_key",
                    acceptance_metadata_value.get(
                        "legacy_source_acceptance_idempotency_key"
                    ),
                )
                if any(
                    value is not None and (not isinstance(value, str) or not value)
                    for value in (
                        source_session_id,
                        source_ingress_id,
                        source_acceptance_key,
                    )
                ):
                    raise ValueError("acceptance source lineage 字段必须是非空字符串")
                connection.execute(
                    "INSERT INTO turn_acceptances(accepted_ingress_id, acceptance_idempotency_key, session_id, turn_id, payload_hash, source_session_id, source_accepted_ingress_id, source_acceptance_idempotency_key, identity_origin, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        accepted_ingress_id,
                        acceptance_idempotency_key,
                        thread_id,
                        turn_value,
                        payload_digest,
                        source_session_id,
                        source_ingress_id,
                        source_acceptance_key,
                        identity_origin,
                        timestamp,
                    ),
                )
                connection.execute(
                    "INSERT INTO turn_records(turn_id, turn_ordinal, source_branch_id, root_input_item_id, root_input_item_sequence, accepted_ingress_id, acceptance_idempotency_key, initial_execution_id, last_execution_id, status, replay_of_turn_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        turn_value,
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
                # acceptance-time 可能早于第一个 LangGraph checkpoint。除了
                # canonical item membership，还必须立即登记 active view 的
                # Turn logical ordinal；否则 replay_as_new_turn/新输入虽然
                # 已经有 root，却会在即时 history/root lookup 中暂时消失，
                # 直到下一次 checkpoint 偶然重建派生索引。
                if active_view is not None and active_view[0] is not None:
                    view_id = strict_text(active_view[0], field="branches.head_view_id")
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
                            "INSERT INTO context_view_turns(view_id, turn_id, logical_turn_ordinal, user_message_sequence, final_message_sequence, root_input_item_id, fork_lineage_json) VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                            (
                                view_id,
                                turn_value,
                                next_turn_ordinal,
                                root.item_id,
                                _json({"source": "acceptance"}),
                            ),
                        )
                        connection.execute(
                            "UPDATE context_views SET head_turn_id = ?, logical_turn_count = MAX(logical_turn_count, ?) WHERE view_id = ?",
                            (turn_value, next_turn_ordinal, view_id),
                        )
                    elif strict_text(
                        existing_view_turn[1],
                        field="context_view_turns.root_input_item_id",
                    ) != root.item_id:
                        raise RuntimeError(
                            f"active view Turn root identity 冲突: turn_id={turn_value}"
                        )
                connection.execute(
                    "INSERT INTO executions(execution_id, turn_id, attempt, execution_ordinal, accepted_ingress_id, outcome, created_at) VALUES (?, ?, 1, 1, ?, 'unknown', ?)",
                    (execution_id, turn_value, accepted_ingress_id, timestamp),
                )
                connection.execute(
                    "INSERT INTO turn_execution_links(turn_id, execution_id, execution_role, execution_ordinal, link_idempotency_key, created_at) VALUES (?, ?, 'initial', 1, ?, ?)",
                    (
                        turn_value,
                        execution_id,
                        f"{turn_value}:initial:{execution_id}",
                        timestamp,
                    ),
                )
                self._commit_connection(connection)
                return {
                    "turn_id": turn_value,
                    "root_input_item_id": root.item_id,
                    "initial_execution_id": execution_id,
                    "commit_id": commit_id,
                    "idempotent": False,
                }
