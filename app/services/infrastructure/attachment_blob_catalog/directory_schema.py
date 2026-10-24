"""目录 catalog 的唯一 schema 4 SQL；当前由离线转换准备，尚未切换线上 owner。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing

from app.core.sqlite_state import load_sql

DIRECTORY_SCHEMA_VERSION = 4
DIRECTORY_SCHEMA_SQL = load_sql(__package__, "sql/managed_directory_schema.sql")
DIRECTORY_SCHEMA_SHA256 = hashlib.sha256(DIRECTORY_SCHEMA_SQL.encode()).hexdigest()


def initialize_directory_schema(connection: sqlite3.Connection) -> None:
    """只初始化新的空目标，不迁移或修复已有库。"""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != 0 or connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
        raise RuntimeError("目录 schema 4 只能在新的空目标初始化，不能覆盖已有 catalog")
    connection.executescript("BEGIN IMMEDIATE;\n" + DIRECTORY_SCHEMA_SQL + "\nCOMMIT;")


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
