"""附件 catalog 已知 schema 4 到 schema 5 的原子 SQLite 转换。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

from app.core.sqlite_state import load_sql
from app.services.infrastructure.attachment_blob_catalog.directory_schema import (
    DIRECTORY_SCHEMA_VERSION,
    initialize_directory_schema,
    require_directory_schema,
)


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
