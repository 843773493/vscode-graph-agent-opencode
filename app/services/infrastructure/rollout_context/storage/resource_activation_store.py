"""thread-owned SQLite 中 activation snapshot 的唯一持久化 owner（9.2）。

本模块是整个 activation catalog 的唯一 writer/reader：

- 只接受 domain 已冻结的 :class:`ResourceActivationSnapshotRef`；不得从当前
  文件、URI、middleware 状态或 generic read 记录补造 provenance/lineage。
- 正文与 source lineage manifest 不进 SQLite；catalog 只保存 manifest、
  typed ref 与 digest，正文由受保护 detail/snapshot body store 持有。
- 正常 runtime 只打开当前 activation schema；marker 缺失或版本不符一律
  fail closed（``resource-activation-schema-unavailable``），不保留旧表读取、
  不动态升级、不留双版本分支。

组合而非继承：本类包装既有 ``RolloutStorage`` 的连接、锁、路径与 v2 校验，
不成为第二个 ContextStore owner。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final, Protocol

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationContractError,
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
    SourceLineageRef,
)
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.storage.resource_activation_schema import (
    RESOURCE_ACTIVATION_MIGRATION_NAME,
    RESOURCE_ACTIVATION_SCHEMA_SQL,
    RESOURCE_ACTIVATION_SCHEMA_VERSION,
    create_resource_activation_schema,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_optional_text,
    strict_text,
)

SCHEMA_UNAVAILABLE_CODE: Final = "resource-activation-schema-unavailable"
LINEAGE_MANIFEST_SCHEMA: Final = "resource-activation-lineage-manifest:v1"

SNAPSHOT_COLUMNS: Final = (
    "session_id",
    "thread_id",
    "activation_snapshot_id",
    "snapshot_kind",
    "parent_turn_snapshot_id",
    "activation_policy_revision",
    "activation_policy_hash",
    "registry_generation",
    "turn_id",
    "model_call_id",
    "captured_at",
    "bindings_hash",
    "activation_provenance_hash",
    "binding_count",
    "lineage_manifest_digest",
    "lineage_detail_ref",
    "created_at",
)

BINDING_COLUMNS: Final = (
    "session_id",
    "thread_id",
    "activation_snapshot_id",
    "activation_ordinal",
    "resource_id",
    "display_uri",
    "resource_kind",
    "owner_scope",
    "facet",
    "revision",
    "availability",
    "content_length",
    "content_hash",
    "redacted_stable_digest",
    "effective_boundary",
    "captured_registry_generation",
    "source_lineage_digest",
    "snapshot_ref",
    "detail_ref",
)

ASSEMBLY_BINDING_COLUMNS: Final = (
    "assembly_id",
    "session_id",
    "thread_id",
    "activation_snapshot_id",
    "plan_id",
    "plan_hash",
    "request_hash",
    "selection_manifest_hash",
    "bindings_hash",
    "activation_provenance_hash",
    "bound_at",
)


class ResourceActivationStoreError(RuntimeError):
    """activation storage 的显式失败；``code`` 是闭合错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


class ResourceActivationLineageBodyStore(Protocol):
    """受保护 lineage body store：catalog 只保存 ref/digest，正文在此。"""

    def write_lineage_manifest(
        self,
        *,
        owner_session_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str,
        manifest: Mapping[str, object],
    ) -> DetailRef: ...

    def read_lineage_manifest(
        self,
        *,
        detail_ref: DetailRef,
        checkpoint_ns: str,
    ) -> Mapping[str, object]: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _optional_detail_ref_from_key(value: object, *, field: str) -> DetailRef | None:
    return None if value is None else _detail_ref_from_key(value, field=field)


def _detail_ref_key(ref: DetailRef, *, field: str) -> str:
    """复用 assembly 的唯一 typed detail key 编码；不接受裸 ID 或路径。"""

    if not isinstance(ref, DetailRef):
        raise ResourceActivationStoreError(
            "resource-activation-schema-invalid", f"{field} 必须是 typed DetailRef"
        )
    return detail_ref_key(ref)


def _detail_ref_from_key(value: object, *, field: str) -> DetailRef:
    return detail_ref_from_key(strict_text(value, field=field))


def lineage_manifest(snapshot: ResourceActivationSnapshotRef) -> dict[str, object]:
    """来源 manifest 的受保护投影：source 向量、derivation 版本与 digest。"""

    return {
        "schema": LINEAGE_MANIFEST_SCHEMA,
        "activation_snapshot_id": snapshot.activation_snapshot_id,
        "lineages": [
            {
                "resource_id": binding.resource_id,
                "activation_ordinal": binding.activation_ordinal,
                "source_lineage_ref": binding.source_lineage_ref.to_dict(),
                "source_lineage_digest": binding.source_lineage_digest,
            }
            for binding in snapshot.bindings
        ],
    }


def lineage_manifest_digest(snapshot: ResourceActivationSnapshotRef) -> str:
    """由内存 snapshot 确定性导出 lineage manifest digest。"""

    return sha256_jcs(lineage_manifest(snapshot))


def _lineage_by_resource(
    manifest: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    if not isinstance(manifest, Mapping) or manifest.get("schema") != (
        LINEAGE_MANIFEST_SCHEMA
    ):
        raise ResourceActivationStoreError(
            "resource-activation-lineage-invalid",
            "受保护 lineage manifest schema 不匹配",
        )
    entries = manifest.get("lineages")
    if not isinstance(entries, list) or not entries:
        raise ResourceActivationStoreError(
            "resource-activation-lineage-invalid",
            "受保护 lineage manifest 缺少 lineages",
        )
    result: dict[str, Mapping[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ResourceActivationStoreError(
                "resource-activation-lineage-invalid", "lineage entry 必须是对象"
            )
        resource_id = strict_text(
            entry.get("resource_id"), field="lineage.resource_id"
        )
        if resource_id in result:
            raise ResourceActivationStoreError(
                "resource-activation-lineage-invalid",
                f"lineage manifest 重复 resource_id: {resource_id}",
            )
        result[resource_id] = entry
    return result


def _source_lineage_ref_for_binding(
    lineages: Mapping[str, Mapping[str, object]],
    *,
    resource_id: str,
    expected_ordinal: int,
    expected_digest: str,
) -> SourceLineageRef:
    entry = lineages.get(resource_id)
    if entry is None:
        raise ResourceActivationStoreError(
            "resource-activation-lineage-invalid",
            f"binding 缺失受保护 lineage manifest: {resource_id}",
        )
    ordinal = strict_non_negative_int(
        entry.get("activation_ordinal"), field="lineage.activation_ordinal"
    )
    if ordinal != expected_ordinal:
        raise ResourceActivationStoreError(
            "resource-activation-lineage-invalid",
            f"lineage ordinal 与 binding 不一致: {resource_id}",
        )
    lineage = SourceLineageRef.from_dict(entry.get("source_lineage_ref"))
    if entry.get("source_lineage_digest") != lineage.digest:
        raise ResourceActivationStoreError(
            "resource-activation-hash-mismatch",
            f"lineage digest 与 lineage ref 不一致: {resource_id}",
        )
    if lineage.digest != expected_digest:
        raise ResourceActivationStoreError(
            "resource-activation-hash-mismatch",
            f"lineage digest 与 binding 列不一致: {resource_id}",
        )
    return lineage


class ResourceActivationStore:
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

    # ---- 读取 -----------------------------------------------------------

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

        with self._storage._connect(
            session_id, checkpoint_ns, read_only=True
        ) as connection:
            self._storage._require_v2_runtime(connection)
            self.require_schema(connection)
            row = connection.execute(
                f"SELECT {','.join(ASSEMBLY_BINDING_COLUMNS)} "
                "FROM resource_activation_assembly_bindings "
                "WHERE assembly_id = ? AND session_id = ?",
                (assembly_id, session_id),
            ).fetchone()
            if row is None:
                raise KeyError(
                    "resource-activation-unavailable: assembly 未绑定 activation "
                    f"snapshot: {assembly_id}"
                )
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
        manifest = self._lineage_body.read_lineage_manifest(
            detail_ref=body_ref, checkpoint_ns=checkpoint_ns
        )
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
