"""附件目录 catalog 的唯一 schema 5 SQL；已知 schema 4 由原 owner 原子转换。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

from app.core.sqlite_state import load_sql

DIRECTORY_SCHEMA_VERSION = 5
DIRECTORY_SCHEMA_SQL = load_sql("app.services.infrastructure.attachment_blob_catalog", "migrations/managed_directory_schema.sql")
DIRECTORY_SCHEMA_SHA256 = hashlib.sha256(DIRECTORY_SCHEMA_SQL.encode()).hexdigest()


def initialize_directory_schema(connection: sqlite3.Connection, *, workspace_id: str | None = None) -> None:
    """只初始化新的空目标，不迁移或修复已有库。"""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != 0 or connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
        raise RuntimeError("目录 schema 5 只能在新的空目标初始化，不能覆盖已有 catalog")
    # executescript 会先提交调用方事务；逐条执行才能把 workspace 归属和 DDL 一起发布。
    connection.execute("BEGIN IMMEDIATE")
    try:
        pending = ""
        for line in DIRECTORY_SCHEMA_SQL.splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                connection.execute(pending)
                pending = ""
        if pending.strip():
            raise RuntimeError("附件目录 SQL 末尾存在未完成语句")
        if workspace_id is not None:
            connection.execute(
                "INSERT INTO attachment_directory_metadata VALUES (1, ?, ?)",
                (workspace_id, DIRECTORY_SCHEMA_VERSION),
            )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def require_directory_schema(connection: sqlite3.Connection) -> None:
    """完整核对版本、约束、索引和触发器；未知形态直接拒绝。"""
    if connection.execute("PRAGMA user_version").fetchone()[0] != DIRECTORY_SCHEMA_VERSION:
        raise RuntimeError("附件目录 catalog 不是 schema 5")
    query = "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    with closing(sqlite3.connect(":memory:")) as expected:
        initialize_directory_schema(expected)
        definitions = {tuple(row) for row in expected.execute(query)}
    if {tuple(row) for row in connection.execute(query)} != definitions:
        raise RuntimeError("附件目录 schema 5 结构不完整或被改写，拒绝修复")


def upgrade_directory_schema_4(connection: sqlite3.Connection, *, workspace_id: str) -> None:
    """原 owner 原子迁齐已知 schema 4；不删除正文，不从未知目录猜测来源。"""
    query = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        # 其它进程可能已先完成同一转换。
        if connection.execute("PRAGMA user_version").fetchone()[0] == 4:
            definitions = sorted(tuple(row) for row in connection.execute(query))
            fingerprint = hashlib.sha256(json.dumps(definitions, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            if fingerprint != "5bbc8a69aa5acf0a5c9adb874d5a4178e950b323fde859e0c9cfe48dfd5f53b4":
                raise RuntimeError("附件目录 schema 4 结构不完整或被改写，保留原库并拒绝转换")
            owner = connection.execute("SELECT workspace_id FROM attachment_directory_metadata WHERE singleton_id=1").fetchone()
            if owner is None or owner[0] != workspace_id:
                raise RuntimeError("附件目录 schema 4 的工作区归属不一致")
            rows = [dict(row) for row in connection.execute("SELECT * FROM attachment_directories")]
            for statement in load_sql("app.services.infrastructure.attachment_blob_catalog", "migrations/upgrade_schema4_directories.sql").split(";"):
                if statement.strip():
                    connection.execute(statement)
            # 新 DDL 仍只来自唯一 SQL 资产，不维护另一套生产 schema。
            with closing(sqlite3.connect(":memory:")) as target:
                initialize_directory_schema(target)
                for name in ("attachment_directory_metadata", "attachment_directories", "attachment_directories_pending"):
                    definition = target.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
                    connection.execute(definition)
            connection.execute("INSERT INTO attachment_directory_metadata VALUES (1,?,?)", (workspace_id, DIRECTORY_SCHEMA_VERSION))
            for row in rows:
                availability = "available" if row["availability"] == "deleting" else row["availability"]
                associated = connection.execute("SELECT 1 FROM attachment_directory_associations WHERE attachment_id=? AND state='active' LIMIT 1", (row["attachment_id"],)).fetchone()
                connection.execute("INSERT INTO attachment_directories VALUES (?,?,?,?,?,?,?,?,?)", (
                    row["attachment_id"], row["relative_locator"], availability, row["protection"], row["created_at"], row["updated_at"],
                    row["retention_until"], int(availability == "available" and associated is None),
                    "旧目录清理中断，须核实原件与内容" if row["availability"] == "deleting" else None,
                ))
            connection.execute(f"PRAGMA user_version={DIRECTORY_SCHEMA_VERSION}")
        require_directory_schema(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("附件目录转换后的关联不完整，拒绝发布")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
