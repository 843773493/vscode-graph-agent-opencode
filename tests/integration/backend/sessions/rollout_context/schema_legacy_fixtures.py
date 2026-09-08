"""schema1/2 的冻结差异 DDL；基础结构来自只读 schema3 历史 fixture。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from tests.integration.backend.sessions.rollout_context.schema_v4_helpers import (
    freeze_schema3_database,
)

OLD_DETAIL_DDL = """CREATE TABLE context_plan_details (
    detail_ref TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '', assembly_id TEXT NOT NULL,
    relative_path TEXT NOT NULL, content_hash TEXT NOT NULL,
    source_revision TEXT, content_length INTEGER, redacted_stable_digest TEXT,
    protection TEXT NOT NULL DEFAULT 'public', availability TEXT NOT NULL DEFAULT 'available',
    required INTEGER NOT NULL, sensitive INTEGER NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, gc_after TEXT
)"""

SCHEMA1_CONTRIBUTIONS_DDL = """CREATE TABLE context_contributions (
    contribution_id TEXT PRIMARY KEY, source_kind TEXT NOT NULL,
    source_revision TEXT NOT NULL, content_hash TEXT NOT NULL, content_length INTEGER,
    redacted_stable_digest TEXT, request_only INTEGER NOT NULL,
    contribution_kind TEXT NOT NULL DEFAULT 'prompt', visibility TEXT NOT NULL DEFAULT 'internal',
    protection TEXT NOT NULL DEFAULT 'public', assembly_id TEXT, contribution_ordinal INTEGER,
    source_ordinal INTEGER, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
)"""

SCHEMA1_ASSEMBLY_CONTRIBUTIONS_DDL = """CREATE TABLE context_assembly_contributions (
    assembly_id TEXT NOT NULL, contribution_ordinal INTEGER NOT NULL,
    contribution_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_revision TEXT NOT NULL,
    content_hash TEXT NOT NULL, content_length INTEGER, redacted_stable_digest TEXT,
    request_only INTEGER NOT NULL, contribution_kind TEXT NOT NULL DEFAULT 'prompt',
    visibility TEXT NOT NULL DEFAULT 'internal', protection TEXT NOT NULL DEFAULT 'public',
    metadata_json TEXT NOT NULL, PRIMARY KEY(assembly_id, contribution_ordinal),
    UNIQUE(assembly_id, contribution_id)
)"""


def stamp_legacy_version(connection: sqlite3.Connection, version: int) -> None:
    """只在冻结 DDL/envelope 已装入之后写匹配 bootstrap；禁止单独假造版本。"""
    assert version in {1, 2}
    assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='context_plans'").fetchone() is None
    columns = {row[1] for row in connection.execute("PRAGMA table_info(context_plan_details)")}
    assert "gc_after" in columns and "detail_id" not in columns
    if version == 1:
        for table in ("context_contributions", "context_assembly_contributions"):
            assert next(row[3] for row in connection.execute(f"PRAGMA table_info({table})") if row[1] == "content_hash") == 1
    connection.execute("UPDATE database_meta SET schema_version=?", (version,))
    name = f"rollout_sqlite_v{version}"
    assert connection.execute("SELECT from_version,to_version FROM schema_migrations").fetchall() == [(0, 3)]
    connection.execute("UPDATE schema_migrations SET to_version=?,migration_name=?,migration_checksum=?", (
        version, name, hashlib.sha256(name.encode()).hexdigest(),
    ))


def freeze_empty_legacy_database(index: Path, *, version: int) -> None:
    """没有 assembly/detail 的 fixture；保留已提交 item/contribution/locator。"""
    freeze_schema3_database(index)
    with closing(sqlite3.connect(index)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM context_plan_details").fetchone() == (0,)
        connection.execute("DROP TABLE context_plan_details")
        connection.execute(OLD_DETAIL_DDL)
        if version == 1:
            for table, ddl in (
                ("context_contributions", SCHEMA1_CONTRIBUTIONS_DDL),
                ("context_assembly_contributions", SCHEMA1_ASSEMBLY_CONTRIBUTIONS_DDL),
            ):
                data = connection.execute(f"SELECT * FROM {table}").fetchall()
                connection.execute(f"DROP TABLE {table}")
                connection.execute(ddl)
                if data:
                    connection.executemany(f"INSERT INTO {table} VALUES({','.join('?' for _ in data[0])})", data)
        stamp_legacy_version(connection, version)
