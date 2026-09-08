"""SQL-only 升级的身份、严格 JSON 和业务指纹。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs

MIGRATION_NAME = "v4_context_plan_registry"
PLAN_TABLES = (
    "context_plans", "context_plan_refs", "context_plan_contributions",
    "context_plan_seal_failures",
)


class SchemaV4UpgradeError(RuntimeError):
    """源事实不完整或目标不等价；由事务 owner 保留原件并回滚。"""


@dataclass(frozen=True)
class ImportSource:
    snapshot: ContextAssemblySnapshot
    snapshot_hash: str
    detail_key: str | None
    checkpoint_ns: str
    seal_key: str
    source_manifest: dict[str, object]

    def provenance(self, audit_id: str) -> dict[str, object]:
        return {
            "source_session_id": self.snapshot.session_id,
            "source_plan_id": self.snapshot.plan_id,
            "source_assembly_id": self.snapshot.assembly_id,
            "source_snapshot_hash": self.snapshot_hash,
            "source_schema_version": 3,
            "audit_id": audit_id,
        }


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def rows(connection: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    cursor = connection.execute(f"SELECT * FROM {quote(table)}")
    columns = tuple(column[0] for column in cursor.description)
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def fingerprint(connection: sqlite3.Connection) -> str:
    """绑定业务数据与 DDL；仅忽略外层升级事务维护的 journal/control 字段。"""
    data: dict[str, object] = {}
    for (table,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        if table in {"schema_migrations", "control_events"}:
            continue
        ignored = set()
        if table == "database_meta":
            ignored = {"schema_version", "database_state", "last_control_sequence", "updated_at"}
        elif table == "context_plans":
            # 新导入时间来自当次 journal.started_at，另由 verify 精确核验；
            # 不将墙钟固化到 SQL，从而保持失败重试的相同 checksum。
            ignored = {"created_at", "updated_at"}
        data[table] = sorted([
            {key: {"sqlite_blob_hex": value.hex()} if isinstance(value, bytes) else value
             for key, value in row.items() if key not in ignored}
            for row in rows(connection, table)
        ], key=canonical_json_bytes)
    data["sqlite_schema"] = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    return sha256_jcs(data)


def parse_snapshot(raw: object) -> ContextAssemblySnapshot:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("重复 JSON key")
            result[key] = value
        return result

    # 错误边界不能把受保护 source metadata 的原值附进异常链。
    try:
        if not isinstance(raw, str):
            raise TypeError("snapshot 必须是 TEXT")
        value = json.loads(raw, object_pairs_hook=unique)
        if canonical_json_bytes(value).decode() != raw:
            raise ValueError("snapshot 不是规范 JCS")
        snapshot = ContextAssemblySnapshot.from_dict(value)
        snapshot.validate_hashes()
        if canonical_json_bytes(snapshot.to_dict()).decode() != raw:
            raise ValueError("snapshot 包含未知字段或非严格持久形态")
    except Exception:  # noqa: BLE001 - 一次性不可信 artifact 的隐私错误边界
        invalid = True
    else:
        invalid = False
    if invalid:
        raise SchemaV4UpgradeError("source-mismatch: schema3 snapshot JSON/schema/hash 非法")
    return snapshot
