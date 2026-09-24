"""activation snapshot 的唯一 SQLite writer/catalog owner（9.2）。

只接受 domain 已冻结的 :class:`ResourceActivationSnapshotRef`；正文与 source
lineage manifest 不进 SQLite，由受保护 detail/snapshot body store 持有。正常
runtime 只打开当前 activation schema，否则 fail closed。

组合而非继承：本类包装既有 ``RolloutStorage`` 的连接、锁、路径与 v2 校验，
不成为第二个 ContextStore owner；只读恢复路径在 ``resource_activation_reads``。
"""

from __future__ import annotations

import hashlib
import sqlite3

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_common import (
    ASSEMBLY_BINDING_COLUMNS,
    BINDING_COLUMNS,
    LINEAGE_MANIFEST_SCHEMA,
    SCHEMA_UNAVAILABLE_CODE,
    SNAPSHOT_COLUMNS,
    ResourceActivationLineageBodyStore,
    ResourceActivationStoreError,
    _detail_ref_from_key,
    _detail_ref_key,
    _now,
    lineage_manifest,
    lineage_manifest_digest,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_reads import (
    ResourceActivationReadMixin,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_schema import (
    RESOURCE_ACTIVATION_MIGRATION_NAME,
    RESOURCE_ACTIVATION_SCHEMA_SQL,
    RESOURCE_ACTIVATION_SCHEMA_VERSION,
    create_resource_activation_schema,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
)


class ResourceActivationStore(ResourceActivationReadMixin):
    """activation snapshot catalog / resource binding manifest / assembly binding。"""

    def __init__(
        self, storage, lineage_body: ResourceActivationLineageBodyStore
    ) -> None:
        self._storage = storage
        self._lineage_body = lineage_body

    # ---- schema 生命周期 -------------------------------------------------

    @staticmethod
    def _checksum() -> str:
        return hashlib.sha256(
            RESOURCE_ACTIVATION_SCHEMA_SQL.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _read_schema_version(connection: sqlite3.Connection) -> int | None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'resource_activation_schema_state'"
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute(
            "SELECT activation_schema_version FROM resource_activation_schema_state "
            "WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise ResourceActivationStoreError(
                "resource-activation-schema-conflict",
                "activation schema 缺少版本 marker row",
            )
        return strict_non_negative_int(
            row[0],
            field="resource_activation_schema_state.activation_schema_version",
        )

    def require_schema(self, connection: sqlite3.Connection) -> None:
        """正常 runtime 只接受当前版本 activation schema，否则 fail closed。"""

        version = self._read_schema_version(connection)
        if version is None:
            raise ResourceActivationStoreError(
                SCHEMA_UNAVAILABLE_CODE,
                "rollout 库尚未建立 activation schema，必须显式 bootstrap/upgrade",
            )
        if version != RESOURCE_ACTIVATION_SCHEMA_VERSION:
            raise ResourceActivationStoreError(
                SCHEMA_UNAVAILABLE_CODE,
                "activation schema 版本不受支持: "
                f"database={version}, supported={RESOURCE_ACTIVATION_SCHEMA_VERSION}",
            )

    def bootstrap_schema(self, connection: sqlite3.Connection) -> None:
        """在调用方事务内建立当前版本 activation schema 与 marker。

        只由显式 bootstrap/一次性迁移入口调用；普通 seal/read 路径不得自动
        升级或补建对象。
        """

        if self._read_schema_version(connection) is not None:
            raise ResourceActivationStoreError(
                "resource-activation-schema-conflict",
                "activation schema 已存在，不能重复 bootstrap",
            )
        create_resource_activation_schema(connection)
        timestamp = _now()
        connection.execute(
            "INSERT INTO resource_activation_schema_state"
            "(singleton_id, activation_schema_version, updated_at) VALUES (1, ?, ?)",
            (RESOURCE_ACTIVATION_SCHEMA_VERSION, timestamp),
        )
        connection.execute(
            "INSERT INTO resource_activation_schema_migrations"
            "(from_version, to_version, migration_name, migration_checksum, status, "
            "started_at, completed_at) VALUES (0, ?, ?, ?, 'completed', ?, ?)",
            (
                RESOURCE_ACTIVATION_SCHEMA_VERSION,
                RESOURCE_ACTIVATION_MIGRATION_NAME,
                self._checksum(),
                timestamp,
                timestamp,
            ),
        )

    # ---- 写入 -----------------------------------------------------------

    def prepare_lineage(
        self,
        snapshot: ResourceActivationSnapshotRef,
        *,
        checkpoint_ns: str = "",
    ) -> DetailRef:
        """先把受保护 lineage manifest 写入 body store，返回 typed DetailRef。

        正文是内容寻址的不可变文件；即使随后的 catalog 事务失败，也只会留下
        可重试的孤儿正文，不会产生指向 catalog 的悬空引用。parent 先递归准备。
        """

        if snapshot.parent is not None:
            self.prepare_lineage(snapshot.parent, checkpoint_ns=checkpoint_ns)
        body_ref = self._lineage_body.write_lineage_manifest(
            owner_session_id=snapshot.owner_session_id,
            activation_snapshot_id=snapshot.activation_snapshot_id,
            checkpoint_ns=checkpoint_ns,
            manifest=lineage_manifest(snapshot),
        )
        if body_ref.session_id != snapshot.owner_session_id:
            raise ResourceActivationStoreError(
                "resource-activation-hash-mismatch",
                "lineage detail ref 与 snapshot owner session 不一致",
            )
        return body_ref

    def persist_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: ResourceActivationSnapshotRef,
        *,
        lineage_detail_ref: DetailRef,
    ) -> None:
        """在既有 catalog 事务内写入 snapshot + binding 行（不提交）。

        调用方持有 rollout owner 写锁与 ``BEGIN IMMEDIATE`` 事务；本方法先递归
        写入 parent，再写自身，保证 FK 顺序。已提交的同 identity 快照按 hash
        幂等复用，不覆盖任何已冻结事实。
        """

        if not connection.in_transaction:
            raise ResourceActivationStoreError(
                "resource-activation-schema-invalid",
                "persist_snapshot 必须在既有事务内执行",
            )
        if not isinstance(lineage_detail_ref, DetailRef):
            raise ResourceActivationStoreError(
                "resource-activation-schema-invalid",
                "lineage_detail_ref 必须是 typed DetailRef",
            )
        if snapshot.parent is not None:
            self.persist_snapshot(
                connection,
                snapshot.parent,
                lineage_detail_ref=lineage_detail_ref,
            )
        digest = lineage_manifest_digest(snapshot)
        existing = self._select_existing(
            connection,
            session_id=snapshot.owner_session_id,
            thread_id=snapshot.owner_thread_id,
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
        if existing is not None:
            self._reuse_existing(existing, snapshot=snapshot, digest=digest)
            return
        timestamp = _now()
        connection.execute(
            "INSERT INTO resource_activation_snapshots "
            f"({','.join(SNAPSHOT_COLUMNS)}) VALUES "
            f"({','.join('?' for _ in SNAPSHOT_COLUMNS)})",
            (
                snapshot.owner_session_id,
                snapshot.owner_thread_id,
                snapshot.activation_snapshot_id,
                snapshot.snapshot_kind,
                snapshot.parent_turn_snapshot_id,
                snapshot.activation_policy_revision,
                snapshot.activation_policy_hash,
                snapshot.registry_generation,
                snapshot.turn_id,
                snapshot.model_call_id,
                snapshot.captured_at,
                snapshot.bindings_hash,
                snapshot.activation_provenance_hash,
                len(snapshot.bindings),
                digest,
                _detail_ref_key(lineage_detail_ref, field="lineage_detail_ref"),
                timestamp,
            ),
        )
        for binding in snapshot.bindings:
            connection.execute(
                "INSERT INTO resource_activation_bindings "
                f"({','.join(BINDING_COLUMNS)}) VALUES "
                f"({','.join('?' for _ in BINDING_COLUMNS)})",
                (
                    snapshot.owner_session_id,
                    snapshot.owner_thread_id,
                    snapshot.activation_snapshot_id,
                    binding.activation_ordinal,
                    binding.resource_id,
                    binding.display_uri,
                    binding.resource_kind,
                    binding.owner_scope,
                    binding.facet,
                    binding.revision,
                    binding.availability,
                    binding.content_length,
                    binding.content_hash,
                    binding.redacted_stable_digest,
                    binding.effective_boundary,
                    binding.captured_registry_generation,
                    binding.source_lineage_digest,
                    (
                        _detail_ref_key(binding.snapshot_ref, field="snapshot_ref")
                        if binding.snapshot_ref is not None
                        else None
                    ),
                    (
                        _detail_ref_key(binding.detail_ref, field="detail_ref")
                        if binding.detail_ref is not None
                        else None
                    ),
                ),
            )

    def save_snapshot(
        self,
        snapshot: ResourceActivationSnapshotRef,
        *,
        checkpoint_ns: str = "",
    ) -> DetailRef:
        """独立事务写入 snapshot + 全部 binding，返回受保护 lineage detail ref。

        seal 路径应改用 :meth:`prepare_lineage` + :meth:`persist_snapshot`，把
        activation 行与 assembly 行放在同一个 ``assembly_sealed`` 事务里。本方法
        只服务需要独立提交的调用方（如 coordinator 的显式保存 port）。
        """

        if not isinstance(snapshot, ResourceActivationSnapshotRef):
            raise TypeError("save_snapshot 只接受 domain ResourceActivationSnapshotRef")
        session_id = snapshot.owner_session_id
        thread_id = snapshot.owner_thread_id
        with self._storage._lock(
            session_id, checkpoint_ns
        ), self._storage._connect(session_id, checkpoint_ns) as connection:
            self._storage._require_v2_runtime(connection)
            self.require_schema(connection)
            existing = self._select_existing(
                connection,
                session_id=session_id,
                thread_id=thread_id,
                activation_snapshot_id=snapshot.activation_snapshot_id,
            )
            if existing is not None:
                return self._reuse_existing(
                    existing,
                    snapshot=snapshot,
                    digest=lineage_manifest_digest(snapshot),
                )
        body_ref = self.prepare_lineage(snapshot, checkpoint_ns=checkpoint_ns)
        with self._storage._lock(
            session_id, checkpoint_ns
        ), self._storage._connect(session_id, checkpoint_ns) as connection:
            self._storage._require_v2_runtime(connection)
            self.require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            self.persist_snapshot(
                connection, snapshot, lineage_detail_ref=body_ref
            )
            connection.commit()
        return body_ref

    @staticmethod
    def _select_existing(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        activation_snapshot_id: str,
    ) -> tuple[object, ...] | None:
        row = connection.execute(
            "SELECT bindings_hash, activation_provenance_hash, "
            "lineage_manifest_digest, lineage_detail_ref "
            "FROM resource_activation_snapshots WHERE session_id = ? "
            "AND thread_id = ? AND activation_snapshot_id = ?",
            (session_id, thread_id, activation_snapshot_id),
        ).fetchone()
        return None if row is None else tuple(row)

    @staticmethod
    def _reuse_existing(
        existing: tuple[object, ...],
        *,
        snapshot: ResourceActivationSnapshotRef,
        digest: str,
    ) -> DetailRef:
        if existing[:2] != (
            snapshot.bindings_hash,
            snapshot.activation_provenance_hash,
        ):
            raise ResourceActivationStoreError(
                "resource-activation-snapshot-conflict",
                "activation_snapshot_id 已存在但 hash 不一致: "
                f"{snapshot.activation_snapshot_id}",
            )
        if existing[2] != digest:
            raise ResourceActivationStoreError(
                "resource-activation-hash-mismatch",
                "已提交 lineage digest 与重算结果不一致: "
                f"{snapshot.activation_snapshot_id}",
            )
        return _detail_ref_from_key(existing[3], field="lineage_detail_ref")

    def bind_assembly(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot: ResourceActivationSnapshotRef,
        assembly_id: str,
        plan_id: str,
        plan_hash: str,
        request_hash: str,
        selection_manifest_hash: str,
    ) -> None:
        """在既有 assembly 事务内原子绑定 selection/plan hash。

        调用方持有 rollout owner 写锁与 ``BEGIN IMMEDIATE`` 事务；本方法只写
        activation assembly binding 行，不提交、不开事务。
        """

        if not connection.in_transaction:
            raise ResourceActivationStoreError(
                "resource-activation-schema-invalid",
                "bind_assembly 必须在既有 assembly 事务内执行",
            )
        for field, value in (
            ("assembly_id", assembly_id),
            ("plan_id", plan_id),
            ("plan_hash", plan_hash),
            ("request_hash", request_hash),
            ("selection_manifest_hash", selection_manifest_hash),
        ):
            strict_text(value, field=f"assembly binding.{field}")
        row = connection.execute(
            "SELECT bindings_hash, activation_provenance_hash "
            "FROM resource_activation_snapshots WHERE session_id = ? "
            "AND thread_id = ? AND activation_snapshot_id = ?",
            (
                snapshot.owner_session_id,
                snapshot.owner_thread_id,
                snapshot.activation_snapshot_id,
            ),
        ).fetchone()
        if row != (
            snapshot.bindings_hash,
            snapshot.activation_provenance_hash,
        ):
            raise ResourceActivationStoreError(
                "resource-activation-hash-mismatch",
                "assembly binding 的 activation snapshot 未提交或 hash 不一致: "
                f"{snapshot.activation_snapshot_id}",
            )
        existing = connection.execute(
            f"SELECT {','.join(ASSEMBLY_BINDING_COLUMNS)} "
            "FROM resource_activation_assembly_bindings WHERE assembly_id = ?",
            (assembly_id,),
        ).fetchone()
        values = (
            assembly_id,
            snapshot.owner_session_id,
            snapshot.owner_thread_id,
            snapshot.activation_snapshot_id,
            plan_id,
            plan_hash,
            request_hash,
            selection_manifest_hash,
            snapshot.bindings_hash,
            snapshot.activation_provenance_hash,
            _now(),
        )
        if existing is not None:
            if tuple(existing) != values:
                raise ResourceActivationStoreError(
                    "resource-activation-snapshot-conflict",
                    f"assembly 已绑定其它 activation snapshot: {assembly_id}",
                )
            return
        connection.execute(
            "INSERT INTO resource_activation_assembly_bindings "
            f"({','.join(ASSEMBLY_BINDING_COLUMNS)}) VALUES "
            f"({','.join('?' for _ in ASSEMBLY_BINDING_COLUMNS)})",
            values,
        )


__all__ = [
    "ASSEMBLY_BINDING_COLUMNS",
    "BINDING_COLUMNS",
    "LINEAGE_MANIFEST_SCHEMA",
    "SCHEMA_UNAVAILABLE_CODE",
    "SNAPSHOT_COLUMNS",
    "ResourceActivationLineageBodyStore",
    "ResourceActivationStore",
    "ResourceActivationStoreError",
    "lineage_manifest",
    "lineage_manifest_digest",
]
