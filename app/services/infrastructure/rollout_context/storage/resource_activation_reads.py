"""activation snapshot 的只读恢复路径（9.2）。

本 mixin 只按稳定 identity 读取 catalog + 受保护 lineage manifest 并逐字节重算
hash；不写任何事实、不读取当前资源、不回退旧 schema。任何篡改、版本不符或
lineage 缺失都必须 fail closed。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationContractError,
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_common import (
    ASSEMBLY_BINDING_COLUMNS,
    BINDING_COLUMNS,
    SNAPSHOT_COLUMNS,
    ResourceActivationStoreError,
    _detail_ref_from_key,
    _lineage_by_resource,
    _optional_detail_ref_from_key,
    _source_lineage_ref_for_binding,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

LINEAGE_BODY_UNAVAILABLE_CODE = "resource-activation-lineage-unavailable"


def activation_tables_ready(connection: sqlite3.Connection) -> bool:
    """activation catalog 是否已 bootstrap；未建立时该库没有 activation 事实。"""

    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'resource_activation_assembly_bindings'"
        ).fetchone()
        is not None
    )


def read_activation_refs_for_turns(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    turn_ids: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    """在同一事务内读取指定 Turn 的 sealed activation 引用摘要。

    rewind/compaction 边界用它把「旧 assembly 的精确 activation identity」写进
    control payload；这里只读已提交事实，不解析当前 URI、不访问 Registry。schema
    未 bootstrap 时返回空，表示该库没有 activation 事实可保留。
    """

    if not turn_ids or not activation_tables_ready(connection):
        return ()
    placeholders = ",".join("?" for _ in turn_ids)
    rows = connection.execute(
        "SELECT b.assembly_id, a.turn_id, b.activation_snapshot_id, "
        "b.plan_hash, b.request_hash, b.bindings_hash, "
        "b.activation_provenance_hash "
        "FROM resource_activation_assembly_bindings b "
        "JOIN context_assemblies a ON a.assembly_id = b.assembly_id "
        "AND a.session_id = b.session_id "
        f"WHERE b.session_id = ? AND a.turn_id IN ({placeholders}) "
        "AND a.status IN ('sealed','terminal') "
        "ORDER BY a.turn_id, b.assembly_id",
        (session_id, *turn_ids),
    ).fetchall()
    return tuple(
        {
            "assembly_id": strict_text(row[0], field="activation_ref.assembly_id"),
            "turn_id": strict_text(row[1], field="activation_ref.turn_id"),
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

        with self._storage._connect(
            session_id, checkpoint_ns, read_only=True
        ) as connection:
            self._storage._require_v2_runtime(connection)
            self.require_schema(connection)
            raw = self._read_raw_snapshot(
                connection,
                session_id=session_id,
                thread_id=thread_id,
                activation_snapshot_id=activation_snapshot_id,
            )
        return self._build_snapshot(raw, checkpoint_ns=checkpoint_ns)

    def read_assembly_binding(
        self,
        session_id: str,
        *,
        assembly_id: str,
        checkpoint_ns: str = "",
    ) -> Mapping[str, object]:
        """读取 assembly 绑定的 activation identity/hash 与 snapshot。"""

        binding = self.find_assembly_binding(
            session_id, assembly_id=assembly_id, checkpoint_ns=checkpoint_ns
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
        assembly_id: str,
        checkpoint_ns: str = "",
    ) -> Mapping[str, object] | None:
        """只读查找 assembly activation 绑定；无 activation 事实时返回 None。

        schema 未 bootstrap 表示该库根本没有 activation 事实（接入前的 seal），
        返回 None 而不报错；一旦 schema 存在但 hash 冲突、snapshot 缺失或正文被
        retention 清理，仍显式失败，绝不静默返回替代 snapshot。
        """

        with self._storage._connect(
            session_id, checkpoint_ns, read_only=True
        ) as connection:
            self._storage._require_v2_runtime(connection)
            if not activation_tables_ready(connection):
                return None
            self.require_schema(connection)
            row = connection.execute(
                f"SELECT {','.join(ASSEMBLY_BINDING_COLUMNS)} "
                "FROM resource_activation_assembly_bindings "
                "WHERE assembly_id = ? AND session_id = ?",
                (assembly_id, session_id),
            ).fetchone()
            if row is None:
                return None
            record = dict(zip(ASSEMBLY_BINDING_COLUMNS, row, strict=True))
            raw = self._read_raw_snapshot(
                connection,
                session_id=session_id,
                thread_id=strict_text(record["thread_id"], field="thread_id"),
                activation_snapshot_id=strict_text(
                    record["activation_snapshot_id"],
                    field="activation_snapshot_id",
                ),
            )
        snapshot = self._build_snapshot(raw, checkpoint_ns=checkpoint_ns)
        if record["bindings_hash"] != snapshot.bindings_hash or record[
            "activation_provenance_hash"
        ] != snapshot.activation_provenance_hash:
            raise ResourceActivationStoreError(
                "resource-activation-hash-mismatch",
                f"assembly binding 与 snapshot hash 不一致: {assembly_id}",
            )
        return {**record, "snapshot": snapshot}

    def _read_raw_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        activation_snapshot_id: str,
    ) -> dict[str, object]:
        """在单个只读连接内读取 snapshot 行 + binding 行 + parent 链原始事实。"""

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
        parent_key = strict_optional_text(
            record["parent_turn_snapshot_id"], field="parent_turn_snapshot_id"
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
        self, raw: Mapping[str, object], *, checkpoint_ns: str
    ) -> ResourceActivationSnapshotRef:
        record = raw["record"]
        assert isinstance(record, dict)
        parent_raw = raw["parent"]
        parent = (
            self._build_snapshot(parent_raw, checkpoint_ns=checkpoint_ns)
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
        body_ref.require_owner(session_id)
        try:
            manifest = self._lineage_body.read_lineage_manifest(
                detail_ref=body_ref, checkpoint_ns=checkpoint_ns
            )
        except (KeyError, DetailUnavailableError) as error:
            # retention/权限/篡改导致正文不可用时必须显式 loss，绝不按同名新资源
            # 或空 manifest 重建 lineage。
            raise ResourceActivationStoreError(
                LINEAGE_BODY_UNAVAILABLE_CODE,
                f"受保护 activation lineage 正文不可用: {activation_snapshot_id}",
            ) from error
        if record["lineage_manifest_digest"] != sha256_jcs(manifest):
            raise ResourceActivationStoreError(
                "resource-activation-hash-mismatch",
                "受保护 lineage manifest digest 与 catalog 不一致: "
                f"{activation_snapshot_id}",
            )
        lineages = _lineage_by_resource(manifest)
        binding_rows = raw["binding_rows"]
        assert isinstance(binding_rows, tuple)
        bindings = tuple(
            self._binding_from_row(row, lineages=lineages) for row in binding_rows
        )
        declared_count = strict_non_negative_int(
            record["binding_count"], field="binding_count"
        )
        if declared_count != len(bindings):
            raise ResourceActivationStoreError(
                "resource-activation-hash-mismatch",
                "binding_count 与实际 binding 行数不一致: "
                f"{activation_snapshot_id}",
            )
        try:
            snapshot = ResourceActivationSnapshotRef.from_dict(
                {
                    "activation_snapshot_id": activation_snapshot_id,
                    "snapshot_kind": record["snapshot_kind"],
                    "parent_turn_snapshot_id": record["parent_turn_snapshot_id"],
                    "activation_policy_revision": record[
                        "activation_policy_revision"
                    ],
                    "activation_policy_hash": record["activation_policy_hash"],
                    "registry_generation": record["registry_generation"],
                    "owner_session_id": session_id,
                    "owner_thread_id": record["thread_id"],
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
        except ResourceActivationContractError as error:
            raise ResourceActivationStoreError(
                "resource-activation-hash-mismatch",
                f"恢复 activation snapshot 失败: {error}",
            ) from error
        if record["bindings_hash"] != snapshot.bindings_hash:
            raise ResourceActivationStoreError(
                "resource-activation-hash-mismatch",
                f"bindings_hash 与重算结果不一致: {activation_snapshot_id}",
            )
        if record["activation_provenance_hash"] != (
            snapshot.activation_provenance_hash
        ):
            raise ResourceActivationStoreError(
                "resource-activation-hash-mismatch",
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
            display_uri=strict_text(record["display_uri"], field="display_uri"),
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
            redacted_stable_digest=strict_optional_text(
                record["redacted_stable_digest"], field="redacted_stable_digest"
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
        )


__all__ = [
    "LINEAGE_BODY_UNAVAILABLE_CODE",
    "ResourceActivationReadMixin",
    "activation_tables_ready",
    "read_activation_refs_for_turns",
]
