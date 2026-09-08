"""显式 schema3→4 SQL-only 升级；不参与正常 read 或初始化。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.assembly.manifest import (
    ContextAssemblyManifestMixin,
)
from app.services.infrastructure.rollout_context.migration.schema_v4.model import (
    MIGRATION_NAME,
    ImportSource,
    SchemaV4UpgradeError,
    fingerprint,
)
from app.services.infrastructure.rollout_context.migration.schema_v4.source import (
    inspect_source,
)
from app.services.infrastructure.rollout_context.migration.schema_v4.sql import (
    literal,
    stage_imports,
)
from app.services.infrastructure.rollout_context.storage.schema_upgrade import (
    validate_schema_journal,
)


@dataclass(frozen=True)
class PreparedSchemaV4Upgrade:
    migration_sql: str
    audit_id: str
    session_id: str
    target_fingerprint: str
    sources: tuple[ImportSource, ...]

    def verify_migrated(self, connection: sqlite3.Connection) -> None:
        """允许提交前事务；只验证，不写 completed audit、不发 COMMIT。"""
        meta = connection.execute(
            "SELECT session_id,schema_version,rollout_format_version,database_state "
            "FROM database_meta WHERE singleton_id=1"
        ).fetchone()
        if meta != (self.session_id, 4, 2, "active"):
            raise SchemaV4UpgradeError("schema-upgrade-state-conflict: 目标不是同 owner 的 active schema4")
        validate_schema_journal(connection, 4)
        checksum = hashlib.sha256(self.migration_sql.encode()).hexdigest()
        journal = connection.execute(
            "SELECT started_at FROM schema_migrations WHERE from_version=3 AND to_version=4 "
            "AND migration_name=? AND migration_checksum=? AND status='completed' "
            "ORDER BY migration_id DESC", (MIGRATION_NAME, checksum),
        ).fetchall()
        if len(journal) != 1 or not isinstance(journal[0][0], str) or not journal[0][0]:
            raise SchemaV4UpgradeError("source-mismatch: 缺少唯一 matching completed schema4 journal")
        timestamps = connection.execute("SELECT created_at,updated_at FROM context_plans").fetchall()
        if any(row != (journal[0][0], journal[0][0]) for row in timestamps):
            raise SchemaV4UpgradeError("source-mismatch: import timestamp 与 schema journal 不一致")
        if fingerprint(connection) != self.target_fingerprint:
            raise SchemaV4UpgradeError("source-mismatch: schema4 业务/来源/DDL 指纹不一致")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SchemaV4UpgradeError("source-mismatch: schema4 registry foreign key")
        validator = ContextAssemblyManifestMixin()
        for source in self.sources:
            validator._validate_context_assembly_manifest(
                connection, source.snapshot, header_detail_ref=source.detail_key,
                checkpoint_ns=source.checkpoint_ns,
            )


def prepare_schema_v4_upgrade(
    connection: sqlite3.Connection, *, session_id: str, checkpoint_ns: str,
) -> PreparedSchemaV4Upgrade:
    """持有 owner 写锁的显式入口调用；只读源连接，DDL/import 在内存中演练。"""
    if connection.in_transaction:
        raise SchemaV4UpgradeError("schema-upgrade-transaction-conflict: prepare 不接受未提交源事务")
    with closing(sqlite3.connect(":memory:")) as target:
        connection.backup(target)
        target.execute("PRAGMA foreign_keys=ON")
        sources = inspect_source(target, session_id=session_id, checkpoint_ns=checkpoint_ns)
        source_fingerprint = fingerprint(target)
        origins = [source.provenance("pending") for source in sources]
        audit_id = sha256_jcs({"schema": "schema3-plan-import:v1", "source": source_fingerprint, "origins": origins})
        binding = {"source_fingerprint": source_fingerprint, "origins": [source.provenance(audit_id) for source in sources]}
        header = "-- schema4-plan-import:" + canonical_json_bytes(binding).decode() + "\n"
        guard = (
            "CREATE TEMP TABLE schema4_assert(ok INTEGER NOT NULL CHECK(ok=1));\n"
            "INSERT INTO schema4_assert VALUES((SELECT COUNT(*)=1 FROM database_meta));\n"
            "INSERT INTO schema4_assert VALUES((SELECT schema_version=3 AND rollout_format_version=2 "
            f"AND session_id={literal(session_id)} FROM database_meta WHERE singleton_id=1));\n"
        )
        script = header + guard + stage_imports(target, sources, audit_id=audit_id) + "\nDROP TABLE schema4_assert;"
        # seal hash 由正式 import port 计算，未来 origin/hash 合同变更不会在迁移
        # 内复制旧算法。新 registry 外的所有旧行/DDL 都被目标指纹绑定。
        return PreparedSchemaV4Upgrade(script, audit_id, session_id, fingerprint(target), sources)


__all__ = ["MIGRATION_NAME", "PreparedSchemaV4Upgrade", "SchemaV4UpgradeError", "prepare_schema_v4_upgrade"]
