"""activation snapshot 的只读恢复路径（9.2）。

本 mixin 只按稳定 identity 读取 catalog + 受保护 lineage manifest 并逐字节重算
hash；不写任何事实、不读取当前资源、不回退旧 schema。任何篡改、版本不符或
lineage 缺失都必须 fail closed。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import nullcontext

from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationContractError,
    ResourceActivationPolicySnapshot,
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)
from app.services.infrastructure.rollout_context.activation.common import (
    ASSEMBLY_BINDING_COLUMNS,
    BINDING_COLUMNS,
    HASH_MISMATCH_CODE,
    LINEAGE_BODY_UNAVAILABLE_CODE,
    SNAPSHOT_COLUMNS,
    SNAPSHOT_CONFLICT_CODE,
    ResourceActivationStoreError,
    _detail_ref_from_key,
    _detail_ref_key,
    _lineage_by_resource,
    _member_bindings_for_resource,
    _optional_detail_ref_from_key,
    _source_lineage_ref_for_binding,
)
from app.services.infrastructure.rollout_context.activation.schema import (
    require_resource_activation_schema,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    current_scope,
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)


def read_activation_refs_for_view(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    thread_id: str,
    view_id: str,
) -> tuple[dict[str, object], ...]:
    """在同一事务内读取 view 两条历史 lane 的 sealed activation 引用摘要。

    rewind/compaction 边界用它把「旧 assembly 的精确 activation identity」写进
    control payload；用户 Turn 与内部 execution 都从明确 membership 取得，
    按原 owner 校验目标本地冻结 scope，不重属化复制的行，不解析当前资源。
    """

    require_resource_activation_schema(connection)
    storage_owner = (session_id, thread_id)
    if current_scope(connection) != storage_owner:
        raise ValueError("source-mismatch: activation ref 读取方与目标 rollout owner 不一致")
    turn_owners = {
        strict_text(row[0], field="activation_ref.turn_id"): (
            strict_text(row[1], field="turn_records.session_id"),
            strict_text(row[2], field="turn_records.thread_id"),
        )
        for row in connection.execute(
            "SELECT membership.turn_id, record.session_id, record.thread_id "
            "FROM context_view_turns membership LEFT JOIN turn_records record "
            "ON record.turn_id=membership.turn_id WHERE membership.view_id=?",
            (view_id,),
        )
    }
    for owner in set(turn_owners.values()):
        require_historical_scope(connection, *owner, current_owner=storage_owner)
    rows = connection.execute(
        "SELECT b.assembly_id, a.turn_id, b.activation_snapshot_id, "
        "b.plan_hash, b.request_hash, b.bindings_hash, "
        "b.activation_provenance_hash, a.session_id, a.thread_id, b.session_id, b.thread_id, "
        "e.session_id, e.thread_id, e.turn_id "
        "FROM resource_activation_assembly_bindings b "
        "JOIN context_assemblies a ON a.assembly_id = b.assembly_id "
        "LEFT JOIN executions e ON e.execution_id=a.execution_id "
        "WHERE (a.turn_id IN (SELECT turn_id FROM context_view_turns WHERE view_id=?) "
        "OR (a.turn_id IS NULL AND EXISTS ("
        "SELECT 1 FROM context_view_items membership JOIN item_catalog item ON item.item_id=membership.item_id "
        "WHERE membership.view_id=? AND membership.visible=1 AND item.turn_id IS NULL "
        "AND item.execution_id=a.execution_id))) "
        "AND a.status IN ('sealed','terminal') "
        "ORDER BY a.turn_id, b.assembly_id",
        (view_id, view_id),
    ).fetchall()
    for row in rows:
        execution_owner = tuple(row[11:13])
        if (
            any(not isinstance(value, str) or not value for value in execution_owner)
            or row[13] != row[1]
            or (row[1] is not None and execution_owner != turn_owners[row[1]])
            or tuple(row[7:9]) != execution_owner
            or tuple(row[9:11]) != execution_owner
        ):
            raise ValueError("source-mismatch: activation ref 的 assembly/binding 与 execution owner 不一致")
        require_historical_scope(connection, *execution_owner, current_owner=storage_owner)
    return tuple(
        {
            "assembly_id": strict_text(row[0], field="activation_ref.assembly_id"),
            "turn_id": strict_optional_text(row[1], field="activation_ref.turn_id"),
            "activation_snapshot_id": strict_text(
                row[2], field="activation_ref.activation_snapshot_id"
            ),
            "plan_hash": strict_text(row[3], field="activation_ref.plan_hash"),
            "request_hash": strict_text(row[4], field="activation_ref.request_hash"),
            "bindings_hash": strict_text(
                row[5], field="activation_ref.bindings_hash"
            ),
            "activation_provenance_hash": strict_text(
                row[6], field="activation_ref.activation_provenance_hash"
            ),
        }
        for row in rows
    )


def binding_assembly_id(
    connection: sqlite3.Connection,
    snapshot: ResourceActivationSnapshotRef,
    binding: ResourceProvenanceRef,
    *,
    storage_owner: tuple[str, str],
    read_lineage_manifest: Callable[[DetailRef], Mapping[str, object]],
) -> str:
    """当前 catalog 验证历史正文归属；继承和重试不改写原 DetailRef。"""
    if not isinstance(snapshot, ResourceActivationSnapshotRef) or not isinstance(
        binding, ResourceProvenanceRef
    ):
        raise TypeError("payload 读取需要 typed activation snapshot 与 binding")
    by_resource = tuple(item for item in snapshot.bindings if item.resource_id == binding.resource_id)
    by_ordinal = tuple(item for item in snapshot.bindings if item.activation_ordinal == binding.activation_ordinal)
    if by_resource != (binding,) or by_ordinal != (binding,):
        raise ResourceActivationStoreError(
            HASH_MISMATCH_CODE,
            "payload binding 的 resource_id/activation_ordinal 不属于 snapshot: "
            f"{binding.resource_id}#{binding.activation_ordinal}",
        )
    root = (
        snapshot.parent
        if snapshot.snapshot_kind == "model_call" and snapshot.parent is not None
        and binding in snapshot.parent.bindings else snapshot
    )
    require_historical_scope(
        connection, root.owner_session_id, root.owner_thread_id, current_owner=storage_owner,
    )
    ref = binding.detail_ref
    if ref is not None:
        ref.require_owner(root.owner_session_id, root.owner_thread_id)
    if ref is None or ref.assembly_id == root.activation_snapshot_id:
        return root.activation_snapshot_id
    if root.turn_id is None or root.snapshot_kind != "execution":
        raise ResourceActivationStoreError(
            HASH_MISMATCH_CODE, "internal/model_call binding 不得借用其它冻结 snapshot 正文",
        )
    # 同真实用户 Turn 的新 execution 只能复用完整相同的旧 binding/lineage。
    row = connection.execute(
        "SELECT b.resource_id, b.activation_ordinal, b.display_uri, b.resource_kind, b.owner_scope, b.facet, "
        "b.revision, b.availability, b.content_length, b.content_hash, b.effective_boundary, "
        "b.captured_registry_generation, b.source_lineage_digest, b.detail_ref, "
        "s.lineage_detail_ref, s.lineage_manifest_digest "
        "FROM resource_activation_snapshots s JOIN resource_activation_bindings b "
        "ON b.session_id=s.session_id AND b.thread_id=s.thread_id "
        "AND b.activation_snapshot_id=s.activation_snapshot_id "
        "WHERE s.session_id=? AND s.thread_id=? AND s.snapshot_kind='execution' "
        "AND s.activation_snapshot_id=? AND s.turn_id=? AND b.resource_id=? AND b.activation_ordinal=?",
        (root.owner_session_id, root.owner_thread_id, ref.assembly_id, root.turn_id,
         binding.resource_id, binding.activation_ordinal),
    ).fetchone()
    expected = (
        binding.resource_id, binding.activation_ordinal, binding.display_uri,
        binding.resource_kind, binding.owner_scope, binding.facet, binding.revision,
        binding.availability, binding.content_length, binding.content_hash,
        binding.effective_boundary, binding.captured_registry_generation,
        binding.source_lineage_digest, _detail_ref_key(ref, field="detail_ref"),
    )
    if row is None or row[:-2] != expected:
        raise ResourceActivationStoreError(
            HASH_MISMATCH_CODE, "retry binding 的原冻结正文关系不一致",
        )
    lineage_ref = _detail_ref_from_key(row[-2], field="lineage_detail_ref")
    lineage_ref.require_owner(root.owner_session_id, root.owner_thread_id, ref.assembly_id)
    manifest = read_lineage_manifest(lineage_ref)
    if sha256_jcs(manifest) != row[-1]:
        raise ResourceActivationStoreError(
            HASH_MISMATCH_CODE, "retry 原冻结 lineage manifest digest 不一致",
        )
    lineages = _lineage_by_resource(manifest)
    lineage = _source_lineage_ref_for_binding(
        lineages, resource_id=binding.resource_id,
        expected_ordinal=binding.activation_ordinal,
        expected_digest=binding.source_lineage_digest,
    )
    if lineage != binding.source_lineage_ref or _member_bindings_for_resource(
        lineages, resource_id=binding.resource_id,
    ) != binding.members:
        raise ResourceActivationStoreError(
            HASH_MISMATCH_CODE, "retry binding 的完整 protected 成员/lineage 事实不一致",
        )
    return ref.assembly_id


class ResourceActivationReadMixin:
    """按稳定 identity 只读恢复 snapshot / assembly binding；不写事实。"""

    def read_snapshot(
        self,
        session_id: str,
        *,
        thread_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str = "",
    ) -> ResourceActivationSnapshotRef:
        """按稳定 identity 读取并重算 hash；篡改 fail closed。"""

        with self._storage.storage._connect(
            self._storage.storage.fork_paths.thread_identity(session_id, thread_id),
            checkpoint_ns,
            session_id=session_id,
            read_only=True,
        ) as connection:
            self._storage.storage._require_v2_runtime(connection)
            self.require_schema(connection)
            raw = self._read_raw_snapshot(
                connection,
                session_id=session_id,
                thread_id=thread_id,
                activation_snapshot_id=activation_snapshot_id,
            )
        return self._build_snapshot(
            raw, read_lineage_manifest=lambda ref: self._lineage_body.read_lineage_manifest(
                detail_ref=ref, checkpoint_ns=checkpoint_ns, storage_owner=(session_id, thread_id),
            ),
        )

    def frozen_for_model_call(
        self,
        session_id: str,
        *,
        thread_id: str,
        model_call_id: str,
        checkpoint_ns: str = "",
    ) -> ResourceActivationSnapshotRef | None:
        """按模型调用的唯一 owner 找回原 ID，不从命名规则重算历史身份。"""

        with self._storage.storage._connect(
            self._storage.storage.fork_paths.thread_identity(session_id, thread_id),
            checkpoint_ns, session_id=session_id, read_only=True,
        ) as connection:
            self._storage.storage._require_v2_runtime(connection)
            self.require_schema(connection)
            row = connection.execute(
                "SELECT activation_snapshot_id FROM resource_activation_snapshots "
                "WHERE session_id = ? AND thread_id = ? "
                "AND snapshot_kind = 'model_call' AND model_call_id = ?",
                (session_id, thread_id, model_call_id),
            ).fetchone()
            if row is None:
                return None
            raw = self._read_raw_snapshot(
                connection, session_id=session_id, thread_id=thread_id,
                activation_snapshot_id=strict_text(row[0], field="activation_snapshot_id"),
            )
        return self._build_snapshot(
            raw, read_lineage_manifest=lambda ref: self._lineage_body.read_lineage_manifest(
                detail_ref=ref, checkpoint_ns=checkpoint_ns, storage_owner=(session_id, thread_id),
            ),
        )

    def frozen_for_admission(
        self, admission: ExecutionAdmission, *, checkpoint_ns: str = ""
    ) -> ResourceActivationSnapshotRef | None:
        """唯一冻结 owner 查询本 execution，或同真实用户 Turn 的首次冻结事实。"""

        with self._storage.storage._connect(
            admission.thread_id, checkpoint_ns, session_id=admission.session_id,
            read_only=True,
        ) as connection:
            self._storage.storage._require_v2_runtime(connection)
            self.require_schema(connection)
            execution = connection.execute(
                "SELECT turn_id, execution_origin, job_id, admission_idempotency_key, "
                "attempt, resumed_from_execution_id, replay_of_execution_id "
                "FROM executions WHERE session_id = ? AND thread_id = ? AND execution_id = ?",
                (admission.session_id, admission.thread_id, admission.execution_id),
            ).fetchone()
            if execution is None or tuple(execution[:4]) != (
                admission.turn_id, admission.execution_origin, admission.job_id,
                admission.admission_idempotency_key,
            ):
                raise ResourceActivationStoreError(
                    SNAPSHOT_CONFLICT_CODE, "activation 查询与持久 admission 不一致"
                )
            row = connection.execute(
                "SELECT activation_snapshot_id FROM resource_activation_snapshots "
                "WHERE session_id = ? AND thread_id = ? AND snapshot_kind = 'execution' "
                "AND execution_id = ?",
                (admission.session_id, admission.thread_id, admission.execution_id),
            ).fetchone()
            if row is None and admission.turn_id is not None:
                row = connection.execute(
                    "SELECT s.activation_snapshot_id FROM resource_activation_snapshots s "
                    "JOIN executions e ON e.execution_id = s.execution_id "
                    "AND e.session_id = s.session_id AND e.thread_id = s.thread_id "
                    "WHERE s.session_id = ? AND s.thread_id = ? AND s.turn_id = ? "
                    "AND s.snapshot_kind = 'execution' ORDER BY e.attempt LIMIT 1",
                    (admission.session_id, admission.thread_id, admission.turn_id),
                ).fetchone()
            if row is None:
                if execution[4] > 1 or any(value is not None for value in execution[5:]):
                    raise ResourceActivationStoreError(
                        SNAPSHOT_CONFLICT_CODE, "retry/resume 缺失原 execution 冻结事实"
                    )
                return None
            raw = self._read_raw_snapshot(
                connection,
                session_id=admission.session_id, thread_id=admission.thread_id,
                activation_snapshot_id=strict_text(row[0], field="activation_snapshot_id"),
            )
        return self._build_snapshot(
            raw, read_lineage_manifest=lambda ref: self._lineage_body.read_lineage_manifest(
                detail_ref=ref, checkpoint_ns=checkpoint_ns,
                storage_owner=(admission.session_id, admission.thread_id),
            ),
        )

    def read_assembly_binding(
        self,
        session_id: str,
        *,
        thread_id: str,
        assembly_id: str,
        checkpoint_ns: str = "",
    ) -> Mapping[str, object]:
        """读取 assembly 绑定的 activation identity/hash 与 snapshot。"""

        binding = self.find_assembly_binding(
            session_id, thread_id=thread_id, assembly_id=assembly_id, checkpoint_ns=checkpoint_ns
        )
        if binding is None:
            raise KeyError(
                "resource-activation-unavailable: assembly 未绑定 activation "
                f"snapshot: {assembly_id}"
            )
        return binding

    def find_assembly_binding(
        self,
        session_id: str,
        *,
        thread_id: str,
        assembly_id: str,
        checkpoint_ns: str = "",
        snapshot: RolloutReadSnapshot | None = None,
    ) -> Mapping[str, object] | None:
        """只读查找 assembly activation 绑定；无 activation 事实时返回 None。

        当前 schema 的无行表示该 assembly 未绑定；缺失/旧 marker、hash 冲突、
        snapshot 缺失或正文被 retention 清理都显式失败。
        """

        thread_id = self._storage.storage.fork_paths.thread_identity(session_id, thread_id)
        with (
            self._storage.storage.open_read_snapshot(session_id, thread_id, checkpoint_ns)
            if snapshot is None else nullcontext(snapshot)
        ) as read_snapshot, self._storage.storage.read_connection(
            session_id, thread_id, checkpoint_ns, snapshot=read_snapshot,
        ) as connection:
            self._storage.storage._require_v2_runtime(connection)
            self.require_schema(connection)
            row = connection.execute(
                f"SELECT {','.join(ASSEMBLY_BINDING_COLUMNS)} "
                "FROM resource_activation_assembly_bindings "
                "WHERE assembly_id = ?",
                (assembly_id,),
            ).fetchone()
            if row is None:
                return None
            record = dict(zip(ASSEMBLY_BINDING_COLUMNS, row, strict=True))
            historical_owner = (
                strict_text(record["session_id"], field="session_id"),
                strict_text(record["thread_id"], field="thread_id"),
            )
            require_historical_scope(
                connection, *historical_owner, current_owner=(session_id, thread_id),
            )
            raw = self._read_raw_snapshot(
                connection,
                session_id=historical_owner[0],
                thread_id=historical_owner[1],
                activation_snapshot_id=strict_text(
                    record["activation_snapshot_id"],
                    field="activation_snapshot_id",
                ),
            )
            assembly_owner = connection.execute(
                "SELECT session_id, thread_id, execution_id, turn_id FROM context_assemblies "
                "WHERE assembly_id = ?",
                (assembly_id,),
            ).fetchone()
            activation = self._build_snapshot(
                raw, read_lineage_manifest=lambda ref: self._lineage_body.read_lineage_manifest(
                    detail_ref=ref, checkpoint_ns=checkpoint_ns,
                    storage_owner=(session_id, thread_id), snapshot=read_snapshot,
                ),
            )
        if assembly_owner != (
            activation.owner_session_id, activation.owner_thread_id,
            activation.execution_id, activation.turn_id,
        ):
            raise ResourceActivationStoreError(
                SNAPSHOT_CONFLICT_CODE, f"assembly 与 activation 真实 owner 不一致: {assembly_id}"
            )
        if record["bindings_hash"] != activation.bindings_hash or record[
            "activation_provenance_hash"
        ] != activation.activation_provenance_hash:
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                f"assembly binding 与 snapshot hash 不一致: {assembly_id}",
            )
        return {**record, "snapshot": activation}

    def _read_raw_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        activation_snapshot_id: str,
    ) -> dict[str, object]:
        """在单个只读连接内读取 snapshot 行 + binding 行 + parent 链原始事实。"""

        require_historical_scope(connection, session_id, thread_id)
        row = connection.execute(
            f"SELECT {','.join(SNAPSHOT_COLUMNS)} "
            "FROM resource_activation_snapshots WHERE session_id = ? "
            "AND thread_id = ? AND activation_snapshot_id = ?",
            (session_id, thread_id, activation_snapshot_id),
        ).fetchone()
        if row is None:
            raise KeyError(
                "resource-activation-unavailable: activation snapshot 不存在: "
                f"{activation_snapshot_id}"
            )
        record = dict(zip(SNAPSHOT_COLUMNS, tuple(row), strict=True))
        execution = connection.execute(
            "SELECT turn_id FROM executions WHERE session_id = ? AND thread_id = ? "
            "AND execution_id = ?",
            (session_id, thread_id, record["execution_id"]),
        ).fetchone()
        if execution != (record["turn_id"],):
            raise ResourceActivationStoreError(
                SNAPSHOT_CONFLICT_CODE, "activation catalog 与真实 execution admission 不一致"
            )
        parent_key = strict_optional_text(
            record["parent_execution_snapshot_id"], field="parent_execution_snapshot_id"
        )
        parent = (
            self._read_raw_snapshot(
                connection,
                session_id=session_id,
                thread_id=thread_id,
                activation_snapshot_id=parent_key,
            )
            if parent_key is not None
            else None
        )
        binding_rows = tuple(
            tuple(binding_row)
            for binding_row in connection.execute(
                f"SELECT {','.join(BINDING_COLUMNS)} "
                "FROM resource_activation_bindings WHERE session_id = ? "
                "AND thread_id = ? AND activation_snapshot_id = ? "
                "ORDER BY activation_ordinal",
                (session_id, thread_id, activation_snapshot_id),
            ).fetchall()
        )
        return {
            "record": record,
            "binding_rows": binding_rows,
            "parent": parent,
        }

    def _build_snapshot(
        self, raw: Mapping[str, object], *,
        read_lineage_manifest: Callable[[DetailRef], Mapping[str, object]],
    ) -> ResourceActivationSnapshotRef:
        record = raw["record"]
        assert isinstance(record, dict)
        parent_raw = raw["parent"]
        parent = (
            self._build_snapshot(
                parent_raw, read_lineage_manifest=read_lineage_manifest,
            )
            if isinstance(parent_raw, dict)
            else None
        )
        session_id = strict_text(record["session_id"], field="session_id")
        activation_snapshot_id = strict_text(
            record["activation_snapshot_id"], field="activation_snapshot_id"
        )
        body_ref = _detail_ref_from_key(
            record["lineage_detail_ref"], field="lineage_detail_ref"
        )
        thread_id = strict_text(record["thread_id"], field="thread_id")
        body_ref.require_owner(session_id, thread_id, activation_snapshot_id)
        try:
            manifest = read_lineage_manifest(body_ref)
        except (KeyError, DetailUnavailableError) as error:
            # retention/权限/篡改导致正文不可用时必须显式 loss，绝不按同名新资源
            # 或空 manifest 重建 lineage。
            raise ResourceActivationStoreError(
                LINEAGE_BODY_UNAVAILABLE_CODE,
                f"受保护 activation lineage 正文不可用: {activation_snapshot_id}",
            ) from error
        if record["lineage_manifest_digest"] != sha256_jcs(manifest):
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                "受保护 lineage manifest digest 与 catalog 不一致: "
                f"{activation_snapshot_id}",
            )
        lineages = _lineage_by_resource(manifest)
        binding_rows = raw["binding_rows"]
        assert isinstance(binding_rows, tuple)
        bindings = tuple(
            self._binding_from_row(row, lineages=lineages) for row in binding_rows
        )
        if set(lineages) != {binding.resource_id for binding in bindings}:
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE, "受保护 lineage manifest 与 binding 资源集合不一致",
            )
        declared_count = strict_non_negative_int(
            record["binding_count"], field="binding_count"
        )
        if declared_count != len(bindings):
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                "binding_count 与实际 binding 行数不一致: "
                f"{activation_snapshot_id}",
            )
        try:
            snapshot = ResourceActivationSnapshotRef.from_dict(
                {
                    "activation_snapshot_id": activation_snapshot_id,
                    "snapshot_kind": record["snapshot_kind"],
                    "parent_execution_snapshot_id": record["parent_execution_snapshot_id"],
                    "activation_policy_revision": record[
                        "activation_policy_revision"
                    ],
                    "activation_policy_hash": record["activation_policy_hash"],
                    "activation_policy": ResourceActivationPolicySnapshot.from_dict(
                        json.loads(strict_text(record["activation_policy_json"], field="activation_policy_json"))
                    ).to_dict(),
                    "registry_generation": record["registry_generation"],
                    "owner_session_id": session_id,
                    "owner_thread_id": record["thread_id"],
                    "execution_id": record["execution_id"],
                    "turn_id": record["turn_id"],
                    "model_call_id": record["model_call_id"],
                    "captured_at": record["captured_at"],
                    "bindings_hash": record["bindings_hash"],
                    "activation_provenance_hash": record[
                        "activation_provenance_hash"
                    ],
                    "bindings": [binding.to_dict() for binding in bindings],
                },
                parent=parent,
            )
        except (ResourceActivationContractError, TypeError, ValueError) as error:
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                f"恢复 activation snapshot 失败: {error}",
            ) from error
        if record["bindings_hash"] != snapshot.bindings_hash:
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                f"bindings_hash 与重算结果不一致: {activation_snapshot_id}",
            )
        if record["activation_provenance_hash"] != (
            snapshot.activation_provenance_hash
        ):
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                "activation_provenance_hash 与重算结果不一致: "
                f"{activation_snapshot_id}",
            )
        return snapshot

    @staticmethod
    def _binding_from_row(
        row: tuple[object, ...], *, lineages: Mapping[str, Mapping[str, object]]
    ) -> ResourceProvenanceRef:
        record = dict(zip(BINDING_COLUMNS, row, strict=True))
        resource_id = strict_text(record["resource_id"], field="resource_id")
        ordinal = strict_non_negative_int(
            record["activation_ordinal"], field="activation_ordinal"
        )
        expected_digest = strict_text(
            record["source_lineage_digest"], field="source_lineage_digest"
        )
        lineage = _source_lineage_ref_for_binding(
            lineages,
            resource_id=resource_id,
            expected_ordinal=ordinal,
            expected_digest=expected_digest,
        )
        return ResourceProvenanceRef(
            resource_id=resource_id,
            display_uri=strict_optional_text(record["display_uri"], field="display_uri"),
            resource_kind=strict_text(record["resource_kind"], field="resource_kind"),
            owner_scope=strict_text(record["owner_scope"], field="owner_scope"),
            facet=strict_text(record["facet"], field="facet"),
            revision=strict_text(record["revision"], field="revision"),
            availability=strict_text(record["availability"], field="availability"),
            content_length=strict_non_negative_int(
                record["content_length"], field="content_length"
            ),
            content_hash=strict_optional_text(
                record["content_hash"], field="content_hash"
            ),
            source_lineage_ref=lineage,
            source_lineage_digest=expected_digest,
            activation_ordinal=ordinal,
            effective_boundary=strict_text(
                record["effective_boundary"], field="effective_boundary"
            ),
            captured_registry_generation=strict_non_negative_int(
                record["captured_registry_generation"],
                field="captured_registry_generation",
            ),
            snapshot_ref=_optional_detail_ref_from_key(
                record["snapshot_ref"], field="snapshot_ref"
            ),
            detail_ref=_optional_detail_ref_from_key(
                record["detail_ref"], field="detail_ref"
            ),
            members=_member_bindings_for_resource(lineages, resource_id=resource_id),
        )


__all__ = [
    "ResourceActivationReadMixin",
    "read_activation_refs_for_view",
]
