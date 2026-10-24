"""附件目录 catalog 的唯一 schema 4 SQL，线上初始化与离线转换共用。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing

from app.core.sqlite_state import load_sql

DIRECTORY_SCHEMA_VERSION = 4
DIRECTORY_SCHEMA_SQL = load_sql(__package__, "sql/managed_directory_schema.sql")
DIRECTORY_SCHEMA_SHA256 = hashlib.sha256(DIRECTORY_SCHEMA_SQL.encode()).hexdigest()


def initialize_directory_schema(connection: sqlite3.Connection, *, workspace_id: str | None = None) -> None:
    """只初始化新的空目标，不迁移或修复已有库。"""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != 0 or connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
        raise RuntimeError("目录 schema 4 只能在新的空目标初始化，不能覆盖已有 catalog")
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
        raise RuntimeError("附件目录 catalog 不是 schema 4")
    query = "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    with closing(sqlite3.connect(":memory:")) as expected:
        initialize_directory_schema(expected)
        definitions = {tuple(row) for row in expected.execute(query)}
    if {tuple(row) for row in connection.execute(query)} != definitions:
        raise RuntimeError("附件目录 schema 4 结构不完整或被改写，拒绝修复")
