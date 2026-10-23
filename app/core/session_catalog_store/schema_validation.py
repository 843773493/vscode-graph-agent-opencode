"""catalog启动与只读备份共用的当前物理schema闸门，不建表或修复数据。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.session_catalog_store._schema import (
    _CATALOG_METADATA_TABLE_DDL,
    _NAVIGATION_QUEUE_DDLS,
    _NAVIGATION_QUEUE_SCHEMA_OBJECTS,
    _NAVIGATION_REVISION_TRIGGER_DDLS,
    _NODES_TABLE_DDL,
    _REQUIRED_NAVIGATION_REVISION_TRIGGERS,
    _REQUIRED_TABLES,
)
from app.core.session_catalog_store.contracts import CatalogMaintenanceRequiredError


def require_catalog_schema(
    connection: sqlite3.Connection, *, database_path: Path, current: int, database_label: str,
) -> None:
    """校验当前 schema 的必需表确实存在；缺表说明库被外部改写。

    绝不允许用 ``CREATE TABLE IF NOT EXISTS`` 把权威表当空表重建：
    ``nodes`` 一空，所有会话位置与父子关系即静默丢失。缺表一律响亮
    失败并列出缺失表名，交由用户从备份恢复或执行维护迁移。
    """
    present = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing = [
        table
        for table in _REQUIRED_TABLES
        if table not in present
    ]
    if missing:
        raise RuntimeError(
            f"{database_label} 缺表，拒绝以空表重建（库被外部改写）: "
            f"path={database_path}, user_version={current}, "
            f"missing={missing}"
        )
    node_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(nodes)").fetchall()
    }
    creation_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(session_creation_records)"
        ).fetchall()
    }
    if (
        not {"title_source", "session_kind", "current_agent_id", "current_provider_id",
             "context_source_session_id", "generation_origin_json", "updated_at"}
        <= node_columns
        or not {"title_source", "purpose", "session_metadata_json", "request_intent_json",
                "graph_id", "graph_revision", "graph_schema_hash", "capability_profile_hash"}
        <= creation_columns
    ):
        raise CatalogMaintenanceRequiredError(
            f"{database_label} v8 缺少权威会话字段，拒绝自动修复: "
            f"path={database_path}, nodes={sorted(node_columns)}, "
            f"creation_records={sorted(creation_columns)}"
        )
    metadata_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(catalog_metadata)"
        ).fetchall()
    }
    missing_columns = {
        "generation",
        "navigation_revision",
    } - metadata_columns
    if missing_columns:
        raise CatalogMaintenanceRequiredError(
            f"{database_label} metadata 缺少必需列，拒绝自动修复: "
            f"path={database_path}, missing={sorted(missing_columns)}"
        )
    metadata_row = connection.execute(
        "SELECT 1 FROM catalog_metadata WHERE singleton_id = 1"
    ).fetchone()
    if metadata_row is None:
        raise CatalogMaintenanceRequiredError(
            f"{database_label} 缺少 catalog_metadata 单例行，"
            "拒绝重置备份或导航 revision: "
            f"path={database_path}"
        )
    canonical = sqlite3.connect(":memory:")
    try:
        canonical.execute(_NODES_TABLE_DDL)
        canonical.execute(_CATALOG_METADATA_TABLE_DDL)
        for trigger_ddl in _NAVIGATION_REVISION_TRIGGER_DDLS:
            canonical.execute(trigger_ddl)
        for queue_ddl in _NAVIGATION_QUEUE_DDLS:
            canonical.execute(queue_ddl)
        canonical_queue_sql = {
            str(row[0]): str(row[1]) for row in canonical.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index')"
            ) if row[0] in _NAVIGATION_QUEUE_SCHEMA_OBJECTS
        }
        canonical_trigger_sql = {
            str(row[0]): str(row[1])
            for row in canonical.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
    finally:
        canonical.close()

    present_queue_sql = {
        str(row[0]): str(row[1]) for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index')"
        ) if row[0] in _NAVIGATION_QUEUE_SCHEMA_OBJECTS
    }
    mismatched_queue_schema = [
        name for name in _NAVIGATION_QUEUE_SCHEMA_OBJECTS
        if present_queue_sql.get(name) != canonical_queue_sql[name]
    ]
    if mismatched_queue_schema:
        raise CatalogMaintenanceRequiredError(
            f"{database_label} navigation queue schema 缺失或定义不符，拒绝自动修复: "
            f"path={database_path}, objects={mismatched_queue_schema}"
        )

    present_trigger_sql = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }
    missing_triggers = (
        set(_REQUIRED_NAVIGATION_REVISION_TRIGGERS)
        - present_trigger_sql.keys()
    )
    mismatched_triggers = {
        name
        for name in _REQUIRED_NAVIGATION_REVISION_TRIGGERS
        if name in present_trigger_sql
        and present_trigger_sql[name] != canonical_trigger_sql[name]
    }
    if missing_triggers or mismatched_triggers:
        raise CatalogMaintenanceRequiredError(
            f"{database_label} 缺少导航 revision triggers 或定义不符，"
            "拒绝继续读写: "
            f"path={database_path}, "
            f"missing={sorted(missing_triggers)}, "
            f"definition_mismatch={sorted(mismatched_triggers)}"
        )
