"""固定来源 bf0f2e1d 的 Session control v18 转为 v20；只消费独立候选。"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from app.core.session_control_store.background_mailbox import (
    BACKGROUND_CONSUMPTIONS_DDL,
)
from app.core.sqlite_state import _execute_migration_sql, load_sql

# 来自固定旧产品实际建库的完整 DDL，不用当前 schema 改版本号伪造旧来源。
_SOURCE_SCHEMA_SHA256 = "d8d69eeef5c07840d83293f498deaf7d06058afa144471437b26e6d6a4a071a1"


def validate_control_v18(connection: sqlite3.Connection) -> None:
    definitions = sorted(tuple(row) for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'",
    ))
    fingerprint = hashlib.sha256(json.dumps(definitions, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if connection.execute("PRAGMA user_version").fetchone()[0] != 18 or fingerprint != _SOURCE_SCHEMA_SHA256:
        raise ValueError("Session control 只支持固定来源的完整 v18 schema，保留原件")
    for table in ("background_consumptions", "background_tasks", "collaboration_members"):
        if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
            raise ValueError(f"Session control v18 含尚未登记转换合同的 {table}，保留原件")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Session control v18 完整性校验失败，保留原件")


def upgrade_control_v18(connection: sqlite3.Connection) -> None:
    validate_control_v18(connection)
    connection.execute("BEGIN IMMEDIATE")
    with connection:
        _execute_migration_sql(connection, load_sql(__package__, "v18_sqlite_to_v20_sqlite.sql"))
        connection.execute(BACKGROUND_CONSUMPTIONS_DDL)
        connection.execute("PRAGMA user_version=20")
