"""execution-owned activation catalog 的唯一当前 DDL；旧实验 schema 不迁移。"""

from __future__ import annotations

import sqlite3

from app.core.sqlite_state import load_sql, utc_now_text
from app.services.infrastructure.rollout_context.activation.common import (
    SCHEMA_CONFLICT_CODE,
    SCHEMA_UNAVAILABLE_CODE,
    ResourceActivationStoreError,
)
from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
)

RESOURCE_ACTIVATION_SCHEMA_VERSION = 4

RESOURCE_ACTIVATION_SCHEMA_SQL = load_sql(__package__, "sql/resource_activation.sql")

RESOURCE_ACTIVATION_TABLES = (
    "resource_activation_schema_state",
    "resource_activation_snapshots",
    "resource_activation_bindings",
    "resource_activation_assembly_bindings",
)


def read_resource_activation_schema_version(connection: sqlite3.Connection) -> int | None:
    """只读版本 marker；缺 row 是损坏，不等于全新库。"""

    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'resource_activation_schema_state'"
    ).fetchone() is None:
        return None
    row = connection.execute(
        "SELECT activation_schema_version FROM resource_activation_schema_state "
        "WHERE singleton_id = 1"
    ).fetchone()
    if row is None:
        raise ResourceActivationStoreError(
            SCHEMA_CONFLICT_CODE, "activation schema 缺少版本 marker row"
        )
    return strict_non_negative_int(row[0], field="activation_schema_version")


def require_resource_activation_schema(connection: sqlite3.Connection) -> None:
    """正常读写、rewind/compaction/fork 都要求当前 schema，绝不补建。"""

    version = read_resource_activation_schema_version(connection)
    if version != RESOURCE_ACTIVATION_SCHEMA_VERSION:
        raise ResourceActivationStoreError(
            SCHEMA_UNAVAILABLE_CODE,
            "activation schema 缺失或版本不受支持: "
            f"database={version}, supported={RESOURCE_ACTIVATION_SCHEMA_VERSION}",
        )
    present = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = set(RESOURCE_ACTIVATION_TABLES) - present
    if missing:
        raise ResourceActivationStoreError(
            SCHEMA_UNAVAILABLE_CODE, f"activation schema 缺少当前对象: {sorted(missing)}"
        )


def create_resource_activation_schema(connection: sqlite3.Connection) -> None:
    """仅 fresh rollout 初始化建立当前 schema；已存在的库必须显式拒绝。"""

    present = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name LIKE 'resource_activation_%'"
    ).fetchall()
    if present:
        raise ResourceActivationStoreError(
            SCHEMA_CONFLICT_CODE, "fresh activation 初始化发现既有实验对象"
        )
    connection.executescript(RESOURCE_ACTIVATION_SCHEMA_SQL)
    connection.execute(
        "INSERT INTO resource_activation_schema_state "
        "(singleton_id, activation_schema_version, updated_at) VALUES (1, ?, ?)",
        (RESOURCE_ACTIVATION_SCHEMA_VERSION, utc_now_text()),
    )


__all__ = [
    "RESOURCE_ACTIVATION_SCHEMA_SQL",
    "RESOURCE_ACTIVATION_SCHEMA_VERSION",
    "RESOURCE_ACTIVATION_TABLES",
    "create_resource_activation_schema",
    "require_resource_activation_schema",
]
