"""v2 sealed context assembly manifest persistence owner。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.enums import CommitKind, CommitMode, TurnStatus
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.identity.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    optional_detail_ref_key,
)
from app.services.infrastructure.rollout_context.assembly.detail_registry import (
    validate_assembly_details,
)
from app.services.infrastructure.rollout_context.assembly.manifest import (
    ContextAssemblyManifestMixin,
)
from app.services.infrastructure.rollout_context.assembly.plans.sealing import (
    bind_sealed_registration,
    preflight_seal,
)
from app.services.infrastructure.rollout_context.assembly.validation import (
    json_text,
    non_negative_int,
    required_text,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
    PreparedDetail,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_optional_text,
    strict_text,
)


def _assert_one_row(cursor, *, context: str) -> None:
    if cursor.rowcount != 1:
        raise RuntimeError(f"{context} 影响行数异常: {cursor.rowcount}")


class ContextAssemblySealMixin(ContextAssemblyManifestMixin):
    def seal_context_assembly(
        self,
        session_id: str,
        thread_id: str,
        snapshot: ContextAssemblySnapshot,
        *,
        seal_idempotency_key: str,
        seal_input_hash: str,
        detail_ref: DetailRef | None = None,
        checkpoint_ns: str = "",
        activation_binding: Callable[[sqlite3.Connection], None] | None = None,
        prepared_details: tuple[PreparedDetail, ...] = (),
        detail_store: ContextPlanDetailStore | None = None,
    ) -> int:
        """在 dispatch 前持久化不可变 assembly snapshot。

        ``activation_binding`` 是 activation coordinator 冻结 snapshot 的显式
        事务参与者：它只在同一个 ``assembly_sealed`` 事务内写入 activation
        snapshot/binding 行，不重读当前资源、不做源 I/O。为 None 时（尚未接入
        activation 的调用方）不写 activation 行，行为与接入前一致。
        """
        detail_key = optional_detail_ref_key(detail_ref)
        if prepared_details and detail_store is None:
            raise ValueError("detail-unavailable: prepared detail 缺少发布 owner")
        if (snapshot.session_id, snapshot.thread_id) != (session_id, thread_id):
            raise ValueError("source-mismatch: assembly seal owner 不一致")
        if detail_ref is not None:
            detail_ref.require_owner(
                snapshot.session_id, snapshot.thread_id, snapshot.assembly_id
            )
        if not isinstance(checkpoint_ns, str):
            raise TypeError("assembly.checkpoint_ns 必须是字符串")
        if not snapshot.sealed:
            raise ValueError("ContextAssemblySnapshot 必须先 seal 再持久化")
        # hash 校验必须发生在 assembly_sealed commit 之前。只依赖恢复时
        # 再校验会让一个伪造的 plan/request hash 短暂成为可 dispatch 的
        # snapshot，并且把错误推迟到重启路径。
        try:
            snapshot.validate_hashes()
        except ItemSchemaError as error:
            raise ValueError(
                f"context assembly hash 校验失败，拒绝 seal: {snapshot.assembly_id}: {error}"
            ) from error
        with self._storage._lock(thread_id, checkpoint_ns, session_id=session_id):
            self._storage.initialize(
                session_id, checkpoint_ns, thread_id=thread_id
            )
            with self._storage._connect(
                thread_id, checkpoint_ns, session_id=session_id
            ) as connection:
                self._storage._require_v2_runtime(connection)
                # 同一个写事务覆盖注册核验、快照/selection 和最终 plan 绑定。
                # 未注册的快照不能绕过 draft 生命周期进入 dispatch。
                connection.execute("BEGIN IMMEDIATE")
                registration = preflight_seal(
                    connection,
                    snapshot,
                    seal_idempotency_key=seal_idempotency_key,
                    seal_input_hash=seal_input_hash,
                    detail_key=detail_key,
                )
                existing = connection.execute(
                    "SELECT plan_hash, request_hash, status, detail_ref, snapshot_json FROM context_assemblies WHERE session_id = ? AND thread_id = ? AND assembly_id = ?",
                    (session_id, thread_id, snapshot.assembly_id),
                ).fetchone()
                if existing is not None:
                    if prepared_details:
                        raise ValueError("assembly-idempotency-conflict: 已提交重试不能发布新 detail")
                    # 损坏快照必须暴露解析错误，不能伪装成普通幂等键冲突。
                    existing_snapshot_matches = canonical_json_bytes(
                        json_text(existing[4], field="snapshot_json")
                    ) == canonical_json_bytes(snapshot.to_dict())
                    existing_plan_hash = strict_text(
                        existing[0], field="context_assemblies.plan_hash"
                    )
                    existing_request_hash = strict_text(
                        existing[1], field="context_assemblies.request_hash"
                    )
                    existing_status = strict_text(
                        existing[2], field="context_assemblies.status"
                    )
                    existing_detail_ref = strict_optional_text(
                        existing[3], field="context_assemblies.detail_ref"
                    )
                    if (
                        existing_plan_hash == snapshot.plan_hash
                        and existing_request_hash == snapshot.request_hash
                        and existing_status in {"sealed", "terminal"}
                        and existing_detail_ref == detail_key
                        and existing_snapshot_matches
                    ):
                        commit_row = connection.execute(
                            "SELECT commit_id, status FROM storage_commits WHERE commit_kind = 'assembly_sealed' AND subject_id = ? AND idempotency_key = ? ORDER BY commit_id LIMIT 1",
                            (
                                snapshot.assembly_id,
                                f"assembly:{snapshot.assembly_id}",
                            ),
                        ).fetchone()
                        if commit_row is None:
                            raise RuntimeError("assembly 已存在但 sealed commit 缺失")
                        if (
                            required_text(commit_row[1], field="commit.status")
                            != "committed"
                        ):
                            raise RuntimeError(
                                "assembly 已存在但 sealed commit 尚未 committed: "
                                f"assembly_id={snapshot.assembly_id} status={commit_row[1]}"
                            )
                        self._validate_context_assembly_manifest(
                            connection, snapshot, registration=registration,
                            header_detail_ref=detail_key, checkpoint_ns=checkpoint_ns,
                        )
                        return non_negative_int(commit_row[0], field="commit_id")
                    raise ValueError("assembly-idempotency-conflict: assembly_id 已存在但输入不一致")
                turn = connection.execute(
                    "SELECT status FROM turn_records WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
                    (snapshot.session_id, snapshot.thread_id, snapshot.turn_id),
                ).fetchone() if snapshot.turn_id is not None else None
                if snapshot.turn_id is not None:
                    if turn is None:
                        raise KeyError(f"assembly Turn 不存在: {snapshot.turn_id}")
                    if required_text(turn[0], field="turn.status") not in {
                        TurnStatus.OPEN.value,
                        TurnStatus.ACTIVE.value,
                    }:
                        raise ValueError(
                            "turn_not_resumable: sealed assembly 只能绑定可 dispatch Turn: "
                            f"{snapshot.turn_id} status={turn[0]}"
                        )
                execution = connection.execute(
                    "SELECT turn_id, execution_origin FROM executions "
                    "WHERE session_id = ? AND thread_id = ? AND execution_id = ?",
                    (snapshot.session_id, snapshot.thread_id, snapshot.execution_id),
                ).fetchone()
                if (
                    execution is None
                    or execution[0] != snapshot.turn_id
                    or required_text(execution[1], field="execution.execution_origin")
                    != ("internal" if snapshot.turn_id is None else "user_turn")
                ):
                    raise ValueError(
                        "assembly execution 不存在或不属于 snapshot session/thread/Turn"
                    )
                prepared_refs = set()
                for prepared in prepared_details:
                    if not isinstance(prepared, PreparedDetail):
                        raise TypeError("assembly detail 必须是 PreparedDetail")
                    prepared.record.detail_ref.require_owner(
                        session_id, thread_id, snapshot.assembly_id,
                    )
                    if prepared.record.detail_ref in prepared_refs:
                        raise ValueError("source-mismatch: prepared detail identity 重复")
                    prepared_refs.add(prepared.record.detail_ref)
                    self._register_context_plan_detail(
                        connection, prepared.record, session_id=session_id,
                        thread_id=thread_id, checkpoint_ns=checkpoint_ns,
                    )
                validate_assembly_details(connection, snapshot, checkpoint_ns, detail_key)
                commit_id, _offset = self._storage._append_v2_records_transaction(
                    connection,
                    thread_id,
                    checkpoint_ns,
                    (),
                    session_id=session_id,
                    commit_kind=CommitKind.ASSEMBLY_SEALED.value,
                    commit_mode=CommitMode.METADATA_ONLY.value,
                    subject_id=snapshot.assembly_id,
                    idempotency_key=f"assembly:{snapshot.assembly_id}",
                    begin_transaction=False,
                    metadata={
                        "session_id": snapshot.session_id,
                        "thread_id": snapshot.thread_id,
                        "assembly_id": snapshot.assembly_id,
                        "plan_hash": snapshot.plan_hash,
                        "request_hash": snapshot.request_hash,
                    },
                )
                timestamp = _now()
                cursor = connection.execute(
                    "INSERT INTO context_assemblies(assembly_id, session_id, thread_id, turn_id, execution_id, plan_id, plan_hash, request_hash, history_view_revision, source_overlay_epoch, snapshot_json, model_call_id, status, detail_ref, created_at, sealed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sealed', ?, ?, ?)",
                    (
                        snapshot.assembly_id,
                        session_id,
                        thread_id,
                        snapshot.turn_id,
                        snapshot.execution_id,
                        snapshot.plan_id,
                        snapshot.plan_hash,
                        snapshot.request_hash,
                        snapshot.history_view_revision,
                        snapshot.source_overlay_epoch,
                        self._assembly_json(snapshot),
                        snapshot.model_call_id,
                        detail_key,
                        timestamp,
                        timestamp,
                    ),
                )
                _assert_one_row(cursor, context="context assembly 写入")
                self._write_context_assembly_manifest(
                    connection, snapshot, checkpoint_ns=checkpoint_ns,
                )
                registration = bind_sealed_registration(
                    connection,
                    snapshot,
                    seal_idempotency_key=seal_idempotency_key,
                    seal_input_hash=seal_input_hash,
                    detail_key=detail_key,
                )
                if activation_binding is not None:
                    # activation 行与 assembly 行必须原子提交：任何一侧失败都在
                    # 同一回滚边界内，绝不留下「assembly 已 dispatch 但无
                    # provenance」或反之的半发布状态。
                    activation_binding(connection)
                self._context.checkpoints._apply_sealed_context_sources(connection, snapshot)
                self._context.checkpoints._consume_context_epoch_transition(connection, snapshot, checkpoint_ns)
                self._validate_context_assembly_manifest(
                    connection, snapshot, registration=registration,
                    header_detail_ref=detail_key, checkpoint_ns=checkpoint_ns,
                )
                published = []
                try:
                    for prepared in prepared_details:
                        published.extend(detail_store.publish(prepared))
                    self._storage._commit_connection(connection)
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    # commit 成功后返回路径抛错也必须保留已提交的正文。
                    committed = connection.execute(
                        "SELECT 1 FROM context_assemblies WHERE assembly_id = ? "
                        "AND session_id = ? AND thread_id = ?",
                        (snapshot.assembly_id, session_id, thread_id),
                    ).fetchone()
                    if committed is None and published:
                        detail_store.discard_published(published)
                    raise
                return commit_id
