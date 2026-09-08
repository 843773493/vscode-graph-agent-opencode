"""仅显式升级入口使用的 COMMIT 后 audit 恢复，不执行 legacy SQL 或初始化。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from app.services.infrastructure.rollout_context.migration.artifacts import (
    read_regular,
    require_safe_path,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.binding import (
    read_prepared_audit,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.journal import (
    PreparedSchemaV3Upgrade,
    digest,
    immutable_file,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.model import (
    SchemaV3UpgradeError,
    object_json,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.publication_reader import (
    read_audit_publication,
)
from app.services.infrastructure.rollout_context.migration.schema_v3.sql import (
    fingerprint,
)


def resume_schema_v3_upgrade_audits(
    connection: sqlite3.Connection, *, rollout_root: Path, checkpoint_ns: str,
) -> bool:
    """调用方持有 owner 锁。schema3 无本 migration audit 时 no-op。"""
    require_safe_path(rollout_root)
    database = connection.execute("PRAGMA database_list").fetchall()
    if len(database) != 1 or Path(database[0][2]) != rollout_root / "index.sqlite":
        raise SchemaV3UpgradeError("source-mismatch: connection 不属于 rollout_root")
    root = rollout_root / "schema-upgrade-v3"
    require_safe_path(root)
    if not root.exists():
        return False
    if connection.in_transaction:
        raise SchemaV3UpgradeError("schema-upgrade-not-committed: audit 恢复不能在事务中执行")
    version = connection.execute("SELECT schema_version,database_state FROM database_meta WHERE singleton_id=1").fetchone()
    if version is None or version[0] < 3:
        raise SchemaV3UpgradeError("schema-upgrade-not-committed: schema2 必须重走 prepare")
    if version[1] == "migrating" and version[0] != 3:
        raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: schema3 audit 不能解除其它版本的 migrating")
    verified = False
    for audit in sorted(root.iterdir()):
        require_safe_path(audit)
        if not audit.is_dir():
            raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: audit 节点不是目录")
        path = audit / "prepared.json"
        if not path.exists():
            # prepare 写入最终 manifest 之前的崩溃不会产生可执行 SQL。
            # 保留不完整 audit；不能把它初始化成可恢复的 Prepared。
            continue
        value, sql = read_prepared_audit(audit, checkpoint_ns=checkpoint_ns)
        matched = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE from_version=2 AND to_version=3 AND status='completed' AND migration_checksum=?",
            (value["migration_checksum"],),
        ).fetchone()
        if matched is None:
            # 早先失败/已放弃的 audit 没有完成的 SQL identity，不予发布。
            continue
        backup = audit / "index.schema2.backup"
        require_safe_path(backup)
        read_regular(backup)
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)) as source:
            if fingerprint(source) != value["source_fingerprint"]:
                raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: source backup fingerprint")
        for relative, expected in value["original_files"].items():
            if digest(read_regular(audit / "originals" / relative)) != expected:
                raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: original backup hash")
        completed = audit / "completed.json"
        if completed.exists():
            completed_raw = read_audit_publication(completed)
            if object_json(completed_raw, field="completed audit") != {
                "audit_id": audit.name, "migration_checksum": value["migration_checksum"],
            }:
                raise SchemaV3UpgradeError("schema-upgrade-audit-conflict: completed identity")
            # 完成后可以有正常新 commit/assembly；不得把当时全库 fingerprint
            # 作为未来每次显式升级的当前数据库状态。
            if version[1] != "migrating":
                immutable_file(completed, completed_raw, retry=True)
                verified = True
                continue
        prepared = PreparedSchemaV3Upgrade(
            sql, audit.name, rollout_root, checkpoint_ns, value["source_fingerprint"],
            value["target_fingerprint"], value["original_files"], value["new_files"], connection,
        )
        prepared.verify_committed(connection)
        verified = True
    return verified


__all__ = ["resume_schema_v3_upgrade_audits"]
