"""activation snapshot 的唯一 SQLite writer/catalog owner（9.2）。

只接受 domain 已冻结的 :class:`ResourceActivationSnapshotRef`；正文与 source
lineage manifest 不进 SQLite，由受保护 detail/snapshot body store 持有。正常
runtime 只打开当前 activation schema，否则 fail closed。

组合而非继承：本类包装既有 ``RolloutContextStore`` 的连接、锁、路径与 v2 校验，
不成为第二个 ContextStore owner；只读恢复路径在 ``resource_activation_reads``。
"""

from __future__ import annotations

import sqlite3

from app.core.sqlite_state import utc_now_text as _now
from app.domain.itemized.hashing import canonical_json_bytes
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
)
from app.services.infrastructure.rollout_context.activation.common import (
    ASSEMBLY_BINDING_COLUMNS,
    BINDING_COLUMNS,
    HASH_MISMATCH_CODE,
    LINEAGE_MANIFEST_SCHEMA,
    SCHEMA_CONFLICT_CODE,
    SCHEMA_INVALID_CODE,
    SCHEMA_UNAVAILABLE_CODE,
    SNAPSHOT_COLUMNS,
    SNAPSHOT_CONFLICT_CODE,
    ResourceActivationLineageBodyStore,
    ResourceActivationStoreError,
    _detail_ref_from_key,
    _detail_ref_key,
    lineage_manifest,
    lineage_manifest_digest,
)
from app.services.infrastructure.rollout_context.activation.reads import (
    ResourceActivationReadMixin,
)
from app.services.infrastructure.rollout_context.activation.schema import (
    require_resource_activation_schema,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
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

    def require_schema(self, connection: sqlite3.Connection) -> None:
        """当前 schema 是唯一读写入口；缺失/旧 marker 不会隐式修复。"""

        require_resource_activation_schema(connection)

    # ---- 写入 -----------------------------------------------------------

    def prepare_lineage(
        self,
        snapshot: ResourceActivationSnapshotRef,
        *,
        checkpoint_ns: str = "",
    ) -> DetailRef:
        """先把受保护 lineage manifest 写入 body store，返回 typed DetailRef。

        正文是内容寻址的不可变文件；catalog 事务失败不会提交半绑定。parent 由独立冻结入口先提交。
        """

        # execution 冻结已经持久化的 lineage 必须精确复用，seal 不能
        # 再分配一份无 catalog 引用的同正文 detail。
        session_id, thread_id = snapshot.owner_session_id, snapshot.owner_thread_id
        with self._storage.storage._lock(
            thread_id, checkpoint_ns, session_id=session_id,
        ), self._storage.storage._connect(
            thread_id, checkpoint_ns, session_id=session_id, read_only=True,
        ) as connection:
            self.require_schema(connection)
            self._require_execution(connection, snapshot)
            existing = self._select_existing(
                connection, session_id=session_id, thread_id=thread_id,
                activation_snapshot_id=snapshot.activation_snapshot_id,
            )
            if existing is not None:
                return self._reuse_existing(
                    existing, snapshot=snapshot, digest=lineage_manifest_digest(snapshot),
                )
        body_ref = self._lineage_body.write_lineage_manifest(
            owner_session_id=snapshot.owner_session_id,
            owner_thread_id=snapshot.owner_thread_id,
            activation_snapshot_id=snapshot.activation_snapshot_id,
            checkpoint_ns=checkpoint_ns,
            manifest=lineage_manifest(snapshot),
        )
        if (body_ref.session_id, body_ref.thread_id) != (
            snapshot.owner_session_id,
            snapshot.owner_thread_id,
        ):
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
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

        调用方持有 rollout owner 写锁与 ``BEGIN IMMEDIATE`` 事务；本方法先核对
        已持久化的 execution parent，再写自身，保证真实 owner 与 FK。已提交的同 identity 快照按 hash
        幂等复用，不覆盖任何已冻结事实。
        """

        if not connection.in_transaction:
            raise ResourceActivationStoreError(
                SCHEMA_INVALID_CODE,
                "persist_snapshot 必须在既有事务内执行",
            )
        if not isinstance(lineage_detail_ref, DetailRef):
            raise ResourceActivationStoreError(
                SCHEMA_INVALID_CODE,
                "lineage_detail_ref 必须是 typed DetailRef",
            )
        self._require_execution(connection, snapshot)
        self._storage.checkpoints.validate_team_activation_observations(connection, snapshot)
        if snapshot.parent is not None:
            parent_row = self._select_existing(
                connection,
                session_id=snapshot.owner_session_id,
                thread_id=snapshot.owner_thread_id,
                activation_snapshot_id=snapshot.parent.activation_snapshot_id,
            )
            if parent_row is None:
                raise ResourceActivationStoreError(
                    SNAPSHOT_CONFLICT_CODE, "model_call 的 execution parent 尚未冻结"
                )
            self._reuse_existing(
                parent_row,
                snapshot=snapshot.parent,
                digest=lineage_manifest_digest(snapshot.parent),
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
                snapshot.parent_execution_snapshot_id,
                snapshot.activation_policy_revision,
                snapshot.activation_policy_hash,
                canonical_json_bytes(snapshot.activation_policy.to_dict()).decode("utf-8"),
                snapshot.registry_generation,
                snapshot.execution_id,
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
        with self._storage.storage._lock(
            thread_id, checkpoint_ns, session_id=session_id
        ), self._storage.storage._connect(
            thread_id, checkpoint_ns, session_id=session_id
        ) as connection:
            self._storage.storage._require_v2_runtime(connection)
            self.require_schema(connection)
            self._require_execution(connection, snapshot)
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
        if snapshot.parent is not None:
            self.save_snapshot(snapshot.parent, checkpoint_ns=checkpoint_ns)
        body_ref = self.prepare_lineage(snapshot, checkpoint_ns=checkpoint_ns)
        with self._storage.storage._lock(
            thread_id, checkpoint_ns, session_id=session_id
        ), self._storage.storage._connect(
            thread_id, checkpoint_ns, session_id=session_id
        ) as connection:
            self._storage.storage._require_v2_runtime(connection)
            self.require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            self.persist_snapshot(
                connection, snapshot, lineage_detail_ref=body_ref
            )
            connection.commit()
        return body_ref

    @staticmethod
    def _require_execution(
        connection: sqlite3.Connection, snapshot: ResourceActivationSnapshotRef
    ) -> None:
        row = connection.execute(
            "SELECT turn_id FROM executions WHERE session_id = ? "
            "AND thread_id = ? AND execution_id = ?",
            (snapshot.owner_session_id, snapshot.owner_thread_id, snapshot.execution_id),
        ).fetchone()
        if row != (snapshot.turn_id,):
            raise ResourceActivationStoreError(
                SNAPSHOT_CONFLICT_CODE,
                f"activation 与真实 execution admission 不一致: {snapshot.execution_id}",
            )

    @staticmethod
    def _select_existing(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        thread_id: str,
        activation_snapshot_id: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            f"SELECT {','.join(SNAPSHOT_COLUMNS)} FROM resource_activation_snapshots "
            "WHERE session_id = ? AND thread_id = ? AND activation_snapshot_id = ?",
            (session_id, thread_id, activation_snapshot_id),
        ).fetchone()
        return None if row is None else dict(zip(SNAPSHOT_COLUMNS, row, strict=True))

    @staticmethod
    def _reuse_existing(
        existing: dict[str, object],
        *,
        snapshot: ResourceActivationSnapshotRef,
        digest: str,
    ) -> DetailRef:
        expected = {
            "snapshot_kind": snapshot.snapshot_kind,
            "parent_execution_snapshot_id": snapshot.parent_execution_snapshot_id,
            "execution_id": snapshot.execution_id,
            "turn_id": snapshot.turn_id,
            "model_call_id": snapshot.model_call_id,
            "activation_policy_revision": snapshot.activation_policy_revision,
            "activation_policy_hash": snapshot.activation_policy_hash,
            "activation_policy_json": canonical_json_bytes(
                snapshot.activation_policy.to_dict()
            ).decode("utf-8"),
            "registry_generation": snapshot.registry_generation,
            "captured_at": snapshot.captured_at,
            "binding_count": len(snapshot.bindings),
            "bindings_hash": snapshot.bindings_hash,
            "activation_provenance_hash": snapshot.activation_provenance_hash,
        }
        if any(existing[key] != entry for key, entry in expected.items()):
            raise ResourceActivationStoreError(
                SNAPSHOT_CONFLICT_CODE,
                f"activation_snapshot_id 已存在但冻结事实不一致: {snapshot.activation_snapshot_id}",
            )
        if existing["lineage_manifest_digest"] != digest:
            raise ResourceActivationStoreError(
                HASH_MISMATCH_CODE,
                f"已提交 lineage digest 与重算结果不一致: {snapshot.activation_snapshot_id}",
            )
        return _detail_ref_from_key(existing["lineage_detail_ref"], field="lineage_detail_ref")

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
                SCHEMA_INVALID_CODE,
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
                HASH_MISMATCH_CODE,
                "assembly binding 的 activation snapshot 未提交或 hash 不一致: "
                f"{snapshot.activation_snapshot_id}",
            )
        assembly = connection.execute(
            "SELECT session_id, thread_id, execution_id, turn_id FROM context_assemblies "
            "WHERE assembly_id = ?",
            (assembly_id,),
        ).fetchone()
        if assembly != (
            snapshot.owner_session_id, snapshot.owner_thread_id,
            snapshot.execution_id, snapshot.turn_id,
        ):
            raise ResourceActivationStoreError(
                SNAPSHOT_CONFLICT_CODE, f"assembly 与真实 activation owner 不一致: {assembly_id}"
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
            if tuple(existing)[:-1] != values[:-1]:
                raise ResourceActivationStoreError(
                    SNAPSHOT_CONFLICT_CODE,
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
    "HASH_MISMATCH_CODE",
    "LINEAGE_MANIFEST_SCHEMA",
    "SCHEMA_CONFLICT_CODE",
    "SCHEMA_INVALID_CODE",
    "SCHEMA_UNAVAILABLE_CODE",
    "SNAPSHOT_COLUMNS",
    "SNAPSHOT_CONFLICT_CODE",
    "ResourceActivationLineageBodyStore",
    "ResourceActivationStore",
    "ResourceActivationStoreError",
    "lineage_manifest",
    "lineage_manifest_digest",
]
