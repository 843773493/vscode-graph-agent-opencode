"""activation snapshot catalog 的唯一 DDL owner（9.2）。

本模块只定义 thread-owned SQLite 中的 activation 对象与版本 marker，不做任何
业务读写。资源正文与 source lineage manifest 不进 SQLite：只有 manifest、
typed ref 与 digest 落库，正文由受保护 detail body store 持有。

版本合同：

- ``RESOURCE_ACTIVATION_SCHEMA_VERSION`` 是当前程序唯一支持的版本。
- 全新库在一次显式 bootstrap 中直接建立当前版本；既有 v2（rollout schema4）
  库必须经一次性 ``upgrade_resource_activation_schema`` 升级，缺失信息写入
  ``resource_activation_migration_losses``，绝不补造 provenance/lineage。
- 正常 runtime 只打开当前版本 marker；marker 缺失或落后一律 fail closed，
  不保留旧表读取、不动态升级、不留双版本分支。
"""

from __future__ import annotations

import sqlite3

RESOURCE_ACTIVATION_SCHEMA_VERSION = 1

RESOURCE_ACTIVATION_MIGRATION_NAME = "resource_activation_schema_v1"

_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS resource_activation_schema_state (
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
        activation_schema_version INTEGER NOT NULL CHECK(activation_schema_version >= 1),
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS resource_activation_schema_migrations (
        migration_id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_version INTEGER NOT NULL,
        to_version INTEGER NOT NULL,
        migration_name TEXT NOT NULL,
        migration_checksum TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('completed','failed')),
        started_at TEXT NOT NULL,
        completed_at TEXT,
        error_message TEXT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS resource_activation_snapshots (
        session_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        activation_snapshot_id TEXT NOT NULL,
        snapshot_kind TEXT NOT NULL CHECK(snapshot_kind IN ('turn','model_call')),
        parent_turn_snapshot_id TEXT,
        activation_policy_revision TEXT NOT NULL,
        activation_policy_hash TEXT NOT NULL,
        registry_generation INTEGER NOT NULL CHECK(registry_generation >= 0),
        turn_id TEXT NOT NULL,
        model_call_id TEXT,
        captured_at TEXT NOT NULL,
        bindings_hash TEXT NOT NULL,
        activation_provenance_hash TEXT NOT NULL,
        binding_count INTEGER NOT NULL CHECK(binding_count >= 1),
        lineage_manifest_digest TEXT NOT NULL,
        lineage_detail_ref TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(session_id, thread_id, activation_snapshot_id),
        FOREIGN KEY(session_id, thread_id, parent_turn_snapshot_id)
            REFERENCES resource_activation_snapshots(
                session_id, thread_id, activation_snapshot_id),
        CHECK((snapshot_kind = 'turn') = (model_call_id IS NULL)),
        CHECK((snapshot_kind = 'turn') = (parent_turn_snapshot_id IS NULL)),
        CHECK(length(activation_policy_revision) > 0),
        CHECK(json_valid(lineage_detail_ref)
            AND json_type(lineage_detail_ref, '$.session_id') IS 'text'
            AND json_type(lineage_detail_ref, '$.assembly_id') IS 'text'
            AND json_type(lineage_detail_ref, '$.detail_id') IS 'text')
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS resource_activation_bindings (
        session_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        activation_snapshot_id TEXT NOT NULL,
        activation_ordinal INTEGER NOT NULL CHECK(activation_ordinal >= 0),
        resource_id TEXT NOT NULL,
        display_uri TEXT NOT NULL,
        resource_kind TEXT NOT NULL,
        owner_scope TEXT NOT NULL,
        facet TEXT NOT NULL,
        revision TEXT NOT NULL,
        availability TEXT NOT NULL,
        content_length INTEGER NOT NULL CHECK(content_length >= 0),
        content_hash TEXT,
        redacted_stable_digest TEXT,
        effective_boundary TEXT NOT NULL CHECK(effective_boundary IN ('turn','model_call')),
        captured_registry_generation INTEGER NOT NULL
            CHECK(captured_registry_generation >= 0),
        source_lineage_digest TEXT NOT NULL,
        snapshot_ref TEXT,
        detail_ref TEXT,
        PRIMARY KEY(session_id, thread_id, activation_snapshot_id, activation_ordinal),
        UNIQUE(session_id, thread_id, activation_snapshot_id, resource_id),
        FOREIGN KEY(session_id, thread_id, activation_snapshot_id)
            REFERENCES resource_activation_snapshots(
                session_id, thread_id, activation_snapshot_id),
        CHECK((content_hash IS NOT NULL) != (redacted_stable_digest IS NOT NULL))
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS resource_activation_assembly_bindings (
        assembly_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        activation_snapshot_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        selection_manifest_hash TEXT NOT NULL,
        bindings_hash TEXT NOT NULL,
        activation_provenance_hash TEXT NOT NULL,
        bound_at TEXT NOT NULL,
        FOREIGN KEY(session_id, thread_id, activation_snapshot_id)
            REFERENCES resource_activation_snapshots(
                session_id, thread_id, activation_snapshot_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS resource_activation_migration_losses (
        loss_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        assembly_id TEXT,
        plan_id TEXT,
        detail_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK(json_valid(detail_json))
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS resource_activation_snapshots_owner_index
        ON resource_activation_snapshots(session_id, thread_id, snapshot_kind, captured_at);
    """,
    """
    CREATE INDEX IF NOT EXISTS resource_activation_snapshots_turn_index
        ON resource_activation_snapshots(session_id, thread_id, turn_id);
    """,
    """
    CREATE INDEX IF NOT EXISTS resource_activation_snapshots_model_call_index
        ON resource_activation_snapshots(session_id, thread_id, model_call_id)
        WHERE model_call_id IS NOT NULL;
    """,
    """
    CREATE INDEX IF NOT EXISTS resource_activation_bindings_resource_index
        ON resource_activation_bindings(
            session_id, thread_id, resource_id, revision);
    """,
    """
    CREATE INDEX IF NOT EXISTS resource_activation_bindings_revision_index
        ON resource_activation_bindings(
            session_id, thread_id, resource_kind, revision);
    """,
    """
    CREATE INDEX IF NOT EXISTS resource_activation_assembly_bindings_snapshot_index
        ON resource_activation_assembly_bindings(session_id, thread_id, activation_snapshot_id);
    """,
    """
    CREATE INDEX IF NOT EXISTS resource_activation_assembly_bindings_plan_index
        ON resource_activation_assembly_bindings(session_id, plan_id, plan_hash);
    """,
    """
    CREATE INDEX IF NOT EXISTS resource_activation_migration_losses_owner_index
        ON resource_activation_migration_losses(session_id, thread_id, reason_code);
    """,
)

RESOURCE_ACTIVATION_SCHEMA_SQL = "\n".join(_TABLE_STATEMENTS)

RESOURCE_ACTIVATION_TABLES: tuple[str, ...] = (
    "resource_activation_schema_state",
    "resource_activation_schema_migrations",
    "resource_activation_snapshots",
    "resource_activation_bindings",
    "resource_activation_assembly_bindings",
    "resource_activation_migration_losses",
)


def create_resource_activation_schema(connection: sqlite3.Connection) -> None:
    """建立 activation 对象；只由 bootstrap 或一次性显式迁移调用。"""

    connection.executescript(RESOURCE_ACTIVATION_SCHEMA_SQL)


__all__ = [
    "RESOURCE_ACTIVATION_MIGRATION_NAME",
    "RESOURCE_ACTIVATION_SCHEMA_SQL",
    "RESOURCE_ACTIVATION_SCHEMA_VERSION",
    "RESOURCE_ACTIVATION_TABLES",
    "create_resource_activation_schema",
]
