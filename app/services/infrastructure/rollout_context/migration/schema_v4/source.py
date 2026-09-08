"""冻结 schema3 输入边界，拒绝有歧义或半初始化的来源。"""

from __future__ import annotations

import hashlib
import sqlite3

from app.services.infrastructure.rollout_context.assembly.plans.imported_sources import (
    build_source_manifest,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.manifest import (
    Schema3AssemblyManifestValidator,
)
from app.services.infrastructure.rollout_context.migration.schema_v4.contributions import (
    source_contributions,
)
from app.services.infrastructure.rollout_context.migration.schema_v4.model import (
    MIGRATION_NAME,
    PLAN_TABLES,
    ImportSource,
    SchemaV4UpgradeError,
    parse_snapshot,
    rows,
)
from app.services.infrastructure.rollout_context.storage.maintenance import (
    RolloutStorageMaintenanceMixin,
)


def inspect_source(
    connection: sqlite3.Connection, *, session_id: str, checkpoint_ns: str,
) -> tuple[ImportSource, ...]:
    if not isinstance(session_id, str) or not session_id.strip():
        raise SchemaV4UpgradeError("source-mismatch: session_id 必须非空")
    if not isinstance(checkpoint_ns, str):
        raise SchemaV4UpgradeError("source-mismatch: checkpoint_ns 必须是字符串")
    meta = connection.execute(
        "SELECT session_id,schema_version,rollout_format_version,database_state "
        "FROM database_meta WHERE singleton_id=1"
    ).fetchone()
    if meta is None or meta[:3] != (session_id, 3, 2):
        raise SchemaV4UpgradeError("schema-upgrade-source-mismatch: 只接受同 owner 的 format2/schema3")
    if meta[3] not in {"active", "recovery_required"}:
        raise SchemaV4UpgradeError("schema-upgrade-state-conflict: 源不是可升级状态")
    RolloutStorageMaintenanceMixin._validate_schema_state(
        connection, allow_older_schema=True,
        pending_retry=(3, 4, MIGRATION_NAME, None) if meta[3] == "recovery_required" else None,
    )
    objects = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    if objects.intersection(PLAN_TABLES):
        raise SchemaV4UpgradeError("schema-upgrade-source-mismatch: schema3 已含部分 plan registry")
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise SchemaV4UpgradeError("source-mismatch: SQLite integrity")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SchemaV4UpgradeError("source-mismatch: SQLite foreign key")
    if connection.execute(
        "SELECT 1 FROM checkpoint_namespace_state WHERE checkpoint_ns=?", (checkpoint_ns,)
    ).fetchone() is None:
        raise SchemaV4UpgradeError("source-mismatch: checkpoint namespace 未注册")
    duplicate = connection.execute(
        "SELECT plan_id,group_concat(assembly_id),COUNT(*) FROM context_assemblies "
        "GROUP BY session_id,plan_id HAVING COUNT(*)>1 ORDER BY plan_id LIMIT 1"
    ).fetchone()
    if duplicate:
        # TODO: 多 assembly 的 plan 需要明确身份重映射合同；本升级不选择最新或合并来源。
        raise SchemaV4UpgradeError(
            f"schema-upgrade-plan-collision: plan={duplicate[0]!r}, assemblies={duplicate[1]!r}"
        )
    result = []
    namespaces: dict[str, set[str]] = {}
    for detail in rows(connection, "context_plan_details"):
        if detail["session_id"] != session_id:
            raise SchemaV4UpgradeError("source-mismatch: detail session owner")
        namespaces.setdefault(detail["assembly_id"], set()).add(detail["checkpoint_ns"])
    validator = Schema3AssemblyManifestValidator()
    for row in sorted(rows(connection, "context_assemblies"), key=lambda row: row["assembly_id"]):
        snapshot = parse_snapshot(row["snapshot_json"])
        fields = ("assembly_id", "session_id", "plan_id", "turn_id", "execution_id",
                  "plan_hash", "request_hash", "history_view_revision", "source_overlay_epoch", "model_call_id")
        if any(row[field] != getattr(snapshot, field) for field in fields) or snapshot.session_id != session_id:
            raise SchemaV4UpgradeError("source-mismatch: assembly header/owner")
        if row["status"] not in {"sealed", "terminal"} or not row["sealed_at"]:
            raise SchemaV4UpgradeError("source-mismatch: assembly 尚未 sealed")
        commit = connection.execute(
            "SELECT idempotency_key,status,commit_mode FROM storage_commits "
            "WHERE commit_kind='assembly_sealed' AND subject_id=?", (snapshot.assembly_id,),
        ).fetchall()
        seal_key = f"assembly:{snapshot.assembly_id}"
        if commit != [(seal_key, "committed", "metadata_only")]:
            raise SchemaV4UpgradeError("source-mismatch: assembly sealed commit 缺失或冲突")
        assembly_ns = namespaces.get(snapshot.assembly_id, {checkpoint_ns})
        if len(assembly_ns) != 1:
            raise SchemaV4UpgradeError("source-mismatch: assembly detail 跨 checkpoint namespace")
        namespace = next(iter(assembly_ns))
        validator._validate_context_assembly_manifest(
            connection, snapshot, header_detail_ref=row["detail_ref"], checkpoint_ns=namespace,
        )
        result.append(ImportSource(
            snapshot, "sha256:jcs:v1:" + hashlib.sha256(row["snapshot_json"].encode()).hexdigest(),
            row["detail_ref"], namespace, seal_key,
            build_source_manifest(
                session_id=session_id, plan_id=snapshot.plan_id, refs=snapshot.refs,
                contributions=source_contributions(connection, snapshot),
            ),
        ))
    for table in ("tool_set_snapshots", "assembly_item_refs", "context_assembly_selections", "context_assembly_contributions"):
        orphan = connection.execute(
            f"SELECT 1 FROM {table} AS child LEFT JOIN context_assemblies AS a "
            "ON child.assembly_id=a.assembly_id WHERE a.assembly_id IS NULL LIMIT 1"
        ).fetchone()
        if orphan:
            raise SchemaV4UpgradeError(f"source-mismatch: 孤立 {table} 行")
    return tuple(result)
