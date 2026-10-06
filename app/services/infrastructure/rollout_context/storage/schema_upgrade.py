"""现行 SQLite bootstrap journal 校验与原子 DDL 执行；不解析旧实验 rollout。"""

from __future__ import annotations

import hashlib
import sqlite3

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
    strict_text,
)


def validate_schema_journal(
    connection: sqlite3.Connection, schema_version: int,
) -> None:
    """校验 bootstrap checksum 与完整提交链；失败审计不能被普通读取越过。"""
    rows = connection.execute(
        "SELECT from_version, to_version, migration_name, migration_checksum, status, completed_at "
        "FROM schema_migrations ORDER BY migration_id",
    ).fetchall()
    if not rows:
        raise RuntimeError("rollout schema_migrations 缺失")
    completed_version = 0
    for index, row in enumerate(rows):
        source = strict_non_negative_int(row[0], field="migration.from_version")
        target = strict_non_negative_int(row[1], field="migration.to_version")
        name = strict_text(row[2], field="migration.name")
        checksum = strict_text(row[3], field="migration.checksum")
        if row[4] not in {"completed", "failed"} or not isinstance(row[5], str) or not row[5]:
            raise RuntimeError("rollout 存在未完成的 SQLite schema migration")
        if source != completed_version or target <= source or (source != 0 and target != source + 1):
            raise RuntimeError("schema-upgrade-journal-conflict: migration 版本链断裂")
        if index == 0 and (source != 0 or row[4] != "completed" or checksum != hashlib.sha256(f"rollout_sqlite_v{target}".encode()).hexdigest()):
            raise RuntimeError("rollout bootstrap schema migration checksum 不匹配")
        if row[4] == "completed":
            completed_version = target
            continue
        contract = (source, target, name, checksum)
        resolved = any(
            tuple(later[:4]) == contract and later[4] == "completed" and later[5]
            for later in rows[index + 1:]
        )
        if resolved:
            continue
        raise RuntimeError("schema-incompatible: rollout 存在未完成的 SQLite schema migration")
    if completed_version != schema_version:
        raise RuntimeError("rollout schema_version 与 schema_migrations 不一致")



def execute_atomic_schema_sql(connection: sqlite3.Connection, script: str) -> None:
    """逐条执行完整 SQLite statement，禁止脚本绕过外层事务边界。"""
    if not connection.in_transaction:
        raise RuntimeError("schema migration 必须在既有事务内执行")

    def authorize(action: int, *_args: object) -> int:
        if action in {
            sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
            sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA,
        }:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        pending: list[str] = []
        for character in script:
            pending.append(character)
            if character == ";" and sqlite3.complete_statement("".join(pending)):
                connection.execute("".join(pending))
                pending.clear()
        remainder = "".join(pending).strip()
        if remainder:
            connection.execute(remainder)
    finally:
        connection.set_authorizer(None)
