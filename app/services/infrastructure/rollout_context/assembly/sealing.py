"""v2 sealed context assembly manifest persistence owner。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import CommitKind, CommitMode, TurnStatus
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes
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
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_optional_text,
    strict_text,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _assert_one_row(cursor, *, context: str) -> None:
    if cursor.rowcount != 1:
        raise RuntimeError(f"{context} 影响行数异常: {cursor.rowcount}")


class ContextAssemblySealMixin(ContextAssemblyManifestMixin):
    def seal_context_assembly(
        self,
        snapshot: ContextAssemblySnapshot,
        *,
        seal_idempotency_key: str,
        seal_input_hash: str,
        detail_ref: DetailRef | None = None,
        checkpoint_ns: str = "",
    ) -> int:
        """在 dispatch 前持久化不可变 assembly snapshot。"""
        detail_key = optional_detail_ref_key(detail_ref)
        if detail_ref is not None:
            detail_ref.require_owner(snapshot.session_id, snapshot.assembly_id)
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
        with self._lock(snapshot.session_id, checkpoint_ns):
            self.initialize(snapshot.session_id, checkpoint_ns)
            with self._connect(snapshot.session_id, checkpoint_ns) as connection:
                self._require_v2_runtime(connection)
                # 同一个写事务覆盖注册核验、快照/selection 和最终 plan 绑定。
                # 未注册的快照不能绕过 draft 生命周期进入 dispatch。
                connection.execute("BEGIN IMMEDIATE")
                preflight_seal(
                    connection,
                    snapshot,
                    seal_idempotency_key=seal_idempotency_key,
                    seal_input_hash=seal_input_hash,
                    detail_key=detail_key,
                )
                existing = connection.execute(
                    "SELECT plan_hash, request_hash, status, detail_ref, snapshot_json FROM context_assemblies WHERE assembly_id = ?",
                    (snapshot.assembly_id,),
                ).fetchone()
                if existing is not None:
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
                            connection, snapshot, header_detail_ref=detail_key,
                            checkpoint_ns=checkpoint_ns,
                        )
                        return non_negative_int(commit_row[0], field="commit_id")
                    raise ValueError("assembly-idempotency-conflict: assembly_id 已存在但输入不一致")
                turn = connection.execute(
                    "SELECT turn_id, status FROM turn_records WHERE turn_id = ?",
                    (snapshot.turn_id,),
                ).fetchone()
                if turn is None:
                    raise KeyError(f"assembly Turn 不存在: {snapshot.turn_id}")
                if required_text(turn[1], field="turn.status") not in {
                    TurnStatus.OPEN.value,
                    TurnStatus.ACTIVE.value,
                }:
                    raise ValueError(
                        "turn_not_resumable: sealed assembly 只能绑定可 dispatch Turn: "
                        f"{snapshot.turn_id} status={turn[1]}"
                    )
                execution = connection.execute(
                    "SELECT turn_id FROM executions WHERE execution_id = ?",
                    (snapshot.execution_id,),
                ).fetchone()
                if (
                    execution is None
                    or required_text(execution[0], field="execution.turn_id")
                    != snapshot.turn_id
                ):
                    raise ValueError("assembly execution 不存在或不属于 snapshot Turn")
                validate_assembly_details(connection, snapshot, checkpoint_ns, detail_key)
                commit_id, _offset = self._append_v2_records_transaction(
                    connection,
                    snapshot.session_id,
                    checkpoint_ns,
                    (),
                    commit_kind=CommitKind.ASSEMBLY_SEALED.value,
                    commit_mode=CommitMode.METADATA_ONLY.value,
                    subject_id=snapshot.assembly_id,
                    idempotency_key=f"assembly:{snapshot.assembly_id}",
                    begin_transaction=False,
                    metadata={
                        "assembly_id": snapshot.assembly_id,
                        "plan_hash": snapshot.plan_hash,
                        "request_hash": snapshot.request_hash,
                    },
                )
                timestamp = _now()
                cursor = connection.execute(
                    "INSERT INTO context_assemblies(assembly_id, session_id, turn_id, execution_id, plan_id, plan_hash, request_hash, history_view_revision, source_overlay_epoch, snapshot_json, model_call_id, status, detail_ref, created_at, sealed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sealed', ?, ?, ?)",
                    (
                        snapshot.assembly_id,
                        snapshot.session_id,
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
                selection_by_identity = {
                    (entry.ref.ref_type, entry.ref.ref_id): entry
                    for entry in snapshot.selection
                }
                for ordinal, ref in enumerate(snapshot.refs):
                    selection_entry = selection_by_identity.get(
                        (ref.ref_type, ref.ref_id)
                    )
                    cursor = connection.execute(
                        "INSERT INTO assembly_item_refs(assembly_id, ref_ordinal, ref_type, ref_id, semantic_kind, payload_kind, status, content_hash, source_revision, content_length, redacted_stable_digest, detail_ref, contribution_id, visibility, protection, availability) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            snapshot.assembly_id,
                            ordinal,
                            ref.ref_type,
                            ref.ref_id,
                            ref.semantic_kind,
                            ref.payload_kind,
                            ref.status,
                            ref.content_hash,
                            ref.source_revision,
                            ref.content_length,
                            ref.redacted_stable_digest,
                            optional_detail_ref_key(selection_entry.detail_ref) if selection_entry else None,
                            selection_entry.contribution_id
                            if selection_entry
                            else None,
                            ref.visibility,
                            ref.protection,
                            ref.availability,
                        ),
                    )
                    _assert_one_row(cursor, context="assembly item ref 写入")
                for ordinal, contribution in enumerate(snapshot.contributions):
                    cursor = connection.execute(
                        "INSERT INTO context_assembly_contributions(assembly_id, contribution_ordinal, contribution_id, source_kind, source_revision, content_hash, content_length, redacted_stable_digest, request_only, contribution_kind, visibility, protection, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            snapshot.assembly_id,
                            contribution.contribution_ordinal
                            if contribution.contribution_ordinal is not None
                            else ordinal,
                            contribution.contribution_id,
                            contribution.source_kind,
                            contribution.source_revision,
                            contribution.content_hash,
                            contribution.content_length,
                            contribution.redacted_stable_digest,
                            int(contribution.request_only),
                            contribution.contribution_kind,
                            contribution.visibility,
                            contribution.protection,
                            _json(dict(contribution.metadata)),
                        ),
                    )
                    _assert_one_row(cursor, context="assembly contribution 写入")
                for entry in snapshot.selection:
                    cursor = connection.execute(
                        "INSERT INTO context_assembly_selections(assembly_id, plan_ordinal, selection_kind, ref_type, ref_id, included, omission_reason, loss_json, source_revision, content_length, content_hash, redacted_stable_digest, visibility, protection, availability, base_delta_role, source_overlay_epoch, overlay_from_revision, overlay_to_revision, overlay_diff_hash, detail_ref, contribution_id, contribution_ordinal) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            snapshot.assembly_id,
                            entry.plan_ordinal,
                            entry.selection_kind,
                            entry.ref.ref_type,
                            entry.ref.ref_id,
                            int(entry.included),
                            entry.omission_reason,
                            _json(list(entry.loss)),
                            entry.source_revision,
                            entry.content_length,
                            entry.content_hash,
                            entry.redacted_stable_digest,
                            entry.visibility,
                            entry.protection,
                            entry.availability,
                            entry.base_delta_role,
                            entry.source_overlay_epoch,
                            entry.overlay_from_revision,
                            entry.overlay_to_revision,
                            entry.overlay_diff_hash,
                            optional_detail_ref_key(entry.detail_ref),
                            entry.contribution_id,
                            entry.contribution_ordinal,
                        ),
                    )
                    _assert_one_row(cursor, context="assembly selection 写入")
                bind_sealed_registration(
                    connection,
                    snapshot,
                    seal_idempotency_key=seal_idempotency_key,
                    seal_input_hash=seal_input_hash,
                    detail_key=detail_key,
                )
                self._validate_context_assembly_manifest(
                    connection, snapshot, header_detail_ref=detail_key,
                    checkpoint_ns=checkpoint_ns,
                )
                self._commit_connection(connection)
                return commit_id
