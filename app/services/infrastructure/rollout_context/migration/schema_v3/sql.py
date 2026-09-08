"""从唯一 schema owner 获取 detail DDL，独立映射 SQL rows 并交叉验证 snapshot。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_key,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.manifest import (
    Schema3AssemblyManifestValidator,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.model import (
    DetailUpgrade,
    SchemaV3UpgradeError,
    object_json,
    rows,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.snapshots import (
    remap_source_metadata,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    initialize_rollout_schema,
)


def fingerprint(connection: sqlite3.Connection) -> str:
    # 除升级事务自己维护的 journal/control 外，绑定全部业务表（含 checkpoint
    # BLOB），不能只检查 detail/assembly 而漏掉原 Turn/view/locator 改动。
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ) if row[0] not in {"database_meta", "schema_migrations", "control_events"}]
    data = {table: sorted([
        {key: {"sqlite_blob_hex": value.hex()} if isinstance(value, bytes) else value
         for key, value in row.items()} for row in rows(connection, table)
    ], key=canonical_json_bytes) for table in tables}
    data["database_meta"] = [
        {key: value for key, value in row.items() if key not in {
            "schema_version", "database_state", "last_control_sequence", "updated_at",
        }} for row in rows(connection, "database_meta")
    ]
    return sha256_jcs(data)


def _literal(value: object) -> str:
    if value is None:
        return "NULL"
    if type(value) is int:
        return str(value)
    if not isinstance(value, str) or "\x00" in value:
        raise SchemaV3UpgradeError("source-mismatch: migration SQL value 不是受支持的 SQLite scalar")
    return "'" + value.replace("'", "''") + "'"


def _update(table: str, old: dict, changes: dict) -> str:
    where = " AND ".join(f'"{key}" IS {_literal(value)}' for key, value in old.items())
    assignments = ",".join(f'"{key}"={_literal(value)}' for key, value in changes.items())
    return f'UPDATE "{table}" SET {assignments} WHERE {where}; INSERT INTO schema3_assert VALUES(changes()=1);'


def build_sql(connection: sqlite3.Connection, details: tuple[DetailUpgrade, ...], snapshots: dict[str, ContextAssemblySnapshot]) -> str:
    with closing(sqlite3.connect(":memory:")) as target:
        initialize_rollout_schema(target)
        ddl = target.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='context_plan_details'").fetchone()[0]
        columns = tuple(row[1] for row in target.execute("PRAGMA table_info(context_plan_details)"))
    mapped = {detail.old_id: detail_ref_key(detail.record.detail_ref) for detail in details}

    def detail_key(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or value not in mapped:
            raise SchemaV3UpgradeError("source-mismatch: SQL detail_ref 未注册")
        return mapped[value]

    statements = [
        "CREATE TEMP TABLE schema3_assert(ok INTEGER CHECK(ok=1))",
        "INSERT INTO schema3_assert SELECT schema_version=2 AND rollout_format_version=2 FROM database_meta WHERE singleton_id=1",
        "DROP TABLE context_plan_details", ddl,
    ]
    for detail in details:
        record = detail.record
        data = {field: getattr(record, field) for field in columns if field not in {"detail_ref", "content_length", "created_at"}}
        data.update(detail_ref=detail_ref_key(record.detail_ref), content_length=record.length, created_at=detail.created_at)
        data["required"], data["sensitive"] = int(record.required), int(record.sensitive)
        statements.append(f"INSERT INTO context_plan_details({','.join(columns)}) VALUES({','.join(_literal(data[field]) for field in columns)})")
    for table in ("assembly_item_refs", "context_assembly_selections"):
        for row in rows(connection, table):
            if row["detail_ref"] is not None:
                statements.append(_update(table, row, {"detail_ref": detail_key(row["detail_ref"])}))
    typed = {detail.old_id: detail.record.detail_ref for detail in details}
    for table in ("context_contributions", "context_assembly_contributions"):
        for row in rows(connection, table):
            value = object_json(row["metadata_json"], field=table + ".metadata_json")
            remapped = remap_source_metadata(value, typed)
            if value != remapped:
                statements.append(_update(table, row, {"metadata_json": canonical_json_bytes(remapped).decode()}))
    for row in rows(connection, "context_assemblies"):
        snapshot = snapshots[row["assembly_id"]]
        statements.append(_update("context_assemblies", row, {
            "detail_ref": detail_key(row["detail_ref"]),
            "snapshot_json": canonical_json_bytes(snapshot.to_dict()).decode(),
            "plan_hash": snapshot.plan_hash, "request_hash": snapshot.request_hash,
        }))
    statements.append("DROP TABLE schema3_assert")
    return ";\n".join(statements) + ";"


def validate_target(connection: sqlite3.Connection, *, checkpoint_ns: str) -> None:
    validator = Schema3AssemblyManifestValidator()
    namespaces: dict[str, set[str]] = {}
    for detail in rows(connection, "context_plan_details"):
        namespaces.setdefault(detail["assembly_id"], set()).add(detail["checkpoint_ns"])
    for row in rows(connection, "context_assemblies"):
        import json

        snapshot = ContextAssemblySnapshot.from_dict(json.loads(row["snapshot_json"]))
        snapshot.validate_hashes()
        if (row["plan_hash"], row["request_hash"]) != (snapshot.plan_hash, snapshot.request_hash):
            raise SchemaV3UpgradeError("source-mismatch: target assembly hash header")
        assembly_namespaces = namespaces.get(row["assembly_id"], {checkpoint_ns})
        if len(assembly_namespaces) != 1:
            raise SchemaV3UpgradeError("source-mismatch: assembly detail 跨 checkpoint namespace")
        validator._validate_context_assembly_manifest(connection, snapshot,
            header_detail_ref=row["detail_ref"], checkpoint_ns=next(iter(assembly_namespaces)))
