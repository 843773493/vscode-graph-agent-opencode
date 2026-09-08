"""由正式 import port 生成新行；只导出明确的新 registry SQL。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from app.services.infrastructure.rollout_context.assembly.plans.imported import (
    write_imported_registration,
)
from app.services.infrastructure.rollout_context.migration.schema_v4.model import (
    MIGRATION_NAME,
    PLAN_TABLES,
    ImportSource,
    SchemaV4UpgradeError,
    quote,
    rows,
)
from app.services.infrastructure.rollout_context.storage.schema_plans import (
    PLAN_REGISTRY_SCHEMA_SQL,
    TOOL_SET_SNAPSHOT_SCHEMA_SQL,
)
from app.services.infrastructure.rollout_context.storage.schema_upgrade import (
    execute_atomic_schema_sql,
)


def literal(value: object) -> str:
    if value is None:
        return "NULL"
    if type(value) is int:
        return str(value)
    if not isinstance(value, str) or "\x00" in value:
        raise SchemaV4UpgradeError("source-mismatch: SQL scalar 不受支持")
    return "'" + value.replace("'", "''") + "'"


def tool_upgrade_sql(connection: sqlite3.Connection) -> str:
    """不复制 DDL；按 schema owner 的列序检查真实 schema3 NOT NULL 工具表。"""
    with closing(sqlite3.connect(":memory:")) as target:
        target.executescript(PLAN_REGISTRY_SCHEMA_SQL + TOOL_SET_SNAPSHOT_SCHEMA_SQL)
        columns = target.execute("PRAGMA table_info(tool_set_snapshots)").fetchall()
    expected = [(*row[:3], 1, *row[4:]) if row[1] == "assembly_id" else row for row in columns]
    if connection.execute("PRAGMA table_info(tool_set_snapshots)").fetchall() != expected:
        raise SchemaV4UpgradeError("schema-upgrade-source-mismatch: 不是冻结 schema3 工具列")
    if connection.execute("PRAGMA foreign_key_list(tool_set_snapshots)").fetchall():
        raise SchemaV4UpgradeError("schema-upgrade-source-mismatch: schema3 工具表不应持有 draft FK")
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE (type='trigger' AND tbl_name='tool_set_snapshots') "
        "OR name='_schema4_tools'"
    ).fetchone():
        raise SchemaV4UpgradeError("schema-upgrade-source-mismatch: 工具表未知 trigger/临时升级表")
    indexes = [row[0] for row in connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='tool_set_snapshots' "
        "AND sql IS NOT NULL ORDER BY name"
    )]
    fields = ",".join(quote(row[1]) for row in columns)
    ddl = TOOL_SET_SNAPSHOT_SCHEMA_SQL.replace("CREATE TABLE tool_set_snapshots", "CREATE TABLE _schema4_tools", 1)
    return "\n".join([
        ddl, f"INSERT INTO _schema4_tools({fields}) SELECT {fields} FROM tool_set_snapshots;",
        "DROP TABLE tool_set_snapshots;", "ALTER TABLE _schema4_tools RENAME TO tool_set_snapshots;",
        *(statement + ";" for statement in indexes),
    ])


def stage_imports(
    target: sqlite3.Connection, sources: tuple[ImportSource, ...], *, audit_id: str,
) -> str:
    """只写 prepare 的内存副本；原 source 绝不传给 import writer。"""
    tools_sql = tool_upgrade_sql(target)
    target.execute("BEGIN IMMEDIATE")
    # failed retry 的业务数据只在内存副本提供给正式 import port；真实状态
    # 的转换及 matching failed journal 校验仍全部属于 storage 事务 owner。
    target.execute("UPDATE database_meta SET database_state='active'")
    execute_atomic_schema_sql(target, PLAN_REGISTRY_SCHEMA_SQL)
    for source in sources:
        write_imported_registration(
            target, source.snapshot, detail_key=source.detail_key,
            origin="schema3_import", source_provenance=source.provenance(audit_id),
            seal_idempotency_key=source.seal_key,
            source_manifest=source.source_manifest,
        )
    # import port 可以服务 fork，但 migration 不允许补造缺失工具行。
    # inspect_source 已验证完整旧工具表；这里只导出新增的三类 registry。
    timestamp = (
        "(SELECT started_at FROM schema_migrations WHERE from_version=3 AND to_version=4 "
        f"AND migration_name={literal(MIGRATION_NAME)} AND status='started' "
        "ORDER BY migration_id DESC LIMIT 1)"
    )
    statements = [PLAN_REGISTRY_SCHEMA_SQL]
    for table in PLAN_TABLES:
        for row in sorted(rows(target, table), key=lambda row: tuple(str(value) for value in row.values())):
            values = [timestamp if table == "context_plans" and key in {"created_at", "updated_at"}
                      else literal(value) for key, value in row.items()]
            statements.append(f"INSERT INTO {quote(table)}({','.join(quote(key) for key in row)}) VALUES({','.join(values)});")
    execute_atomic_schema_sql(target, tools_sql)
    statements.append(tools_sql)
    return "\n".join(statements)
