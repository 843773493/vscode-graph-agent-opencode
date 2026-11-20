"""rollout v2 SQLite schema 的唯一建表 owner。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from app.core.sqlite_state import _execute_migration_sql, load_sql
from app.services.infrastructure.rollout_context.storage.schema_assembly_refs import (
    ASSEMBLY_REF_SCHEMA_SQL,
)
from app.services.infrastructure.rollout_context.storage.schema_channel_versions import (
    CHANNEL_VALUES_SCHEMA_SQL,
)
from app.services.infrastructure.rollout_context.storage.schema_plans import (
    PLAN_REGISTRY_SCHEMA_SQL,
    TOOL_SET_SNAPSHOT_SCHEMA_SQL,
)
from app.services.infrastructure.rollout_context.storage.schema_wakeup import (
    RUNTIME_WAKEUP_SCHEMA_SQL,
)

ROLLOUT_SCHEMA_VERSION = 28
ITEM_CATALOG_LOCATOR_INDEX_SQL = load_sql(__package__, "migrations/catalog/item_catalog_locator_index.sql")
COMMIT_IDEMPOTENCY_INDEX_SQL = load_sql(__package__, "migrations/catalog/commit_idempotency_index.sql")
ITEM_CATALOG_MODEL_TOOL_INDEX_SQL = load_sql(__package__, "migrations/catalog/item_catalog_model_tool_index.sql")
MODEL_CALLS_ASSEMBLY_INDEX_SQL = load_sql(__package__, "migrations/assembly/model_calls_assembly_index.sql")
CONTEXT_CONTRIBUTIONS_SOURCE_REF_INDEX_SQL = load_sql(__package__, "migrations/context_sources/context_contributions_source_ref_index.sql")
CONTEXT_CONTRIBUTIONS_OVERLAY_INDEX_SQL = load_sql(__package__, "migrations/context_sources/context_contributions_overlay_index.sql")
SOURCE_OVERLAY_LOCATOR_INDEXES_SQL = load_sql(__package__, "migrations/context_sources/source_overlay_locator_indexes.sql")
TOOL_CALLS_MESSAGE_INDEX_SQL = load_sql(__package__, "migrations/catalog/tool_calls_message_index.sql")
EXECUTION_QUEUE_SCHEMA_SQL = load_sql(__package__, "migrations/execution/execution_queue.sql")
EXECUTION_SCHEDULING_MIGRATION_SQL = load_sql(__package__, "migrations/execution/execution_queue_026_027.sql")
TEAM_OBSERVATIONS_MIGRATION_SQL = load_sql(__package__, "migrations/context_sources/team_observations_027_028.sql")
SKILL_SELECTION_MIGRATION_SQL = load_sql("app.services.infrastructure.rollout_context.storage", "migrations/context_sources/skill_selection_024_025.sql")
VIEW_CREATION_SCHEMA_SQL = load_sql(__package__, "migrations/views/view_creation.sql")
VIEW_CREATION_MIGRATION_SQL = load_sql(__package__, "migrations/views/view_creation_025_026.sql") + VIEW_CREATION_SCHEMA_SQL

MESSAGE_FORMAT_VERSION = 1
ROLLOUT_FORMAT_VERSION = 2

ASSEMBLY_CONTENT_SCHEMA_SQL = load_sql(__package__, "migrations/assembly/assembly_content.sql")
VIEW_MEMBERSHIP_SCHEMA_SQL = load_sql(__package__, "migrations/views/view_membership.sql")
VIEW_MEMBERSHIP_MIGRATION_SQL = load_sql(__package__, "migrations/views/view_membership_022_023_prepare.sql") + VIEW_MEMBERSHIP_SCHEMA_SQL

MESSAGES_SCHEMA_SQL = load_sql(__package__, "migrations/checkpoint/messages.sql")
MESSAGES_VERSION_MIGRATION_SQL = (
    load_sql(__package__, "migrations/checkpoint/messages_020_021_prepare.sql")
    + MESSAGES_SCHEMA_SQL
    + load_sql(__package__, "migrations/checkpoint/messages_020_021.sql")
)

HISTORY_NAMESPACE_SCHEMA_SQL = load_sql(__package__, "migrations/history/history_namespace.sql")
"""复制历史的冻结 namespace；正文与历史业务 hash 不因当前归属改变。"""

ITEM_RESOURCE_REFERENCES_SCHEMA_SQL = load_sql(__package__, "migrations/catalog/item_resource_references.sql")
"""canonical item 的 typed 引用索引；资源生命周期仍由各资源 owner 管理。"""

CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_STATEMENTS: tuple[str, ...] = (
    load_sql(__package__, "migrations/context_sources/context_source_control_states.sql"),
    load_sql(__package__, "migrations/context_sources/context_source_control_states_owner_index.sql"),
    load_sql(__package__, "migrations/context_sources/team_source_observations.sql"),
)
"""CSM 控制状态的唯一 DDL 定义；新库与既有 v4 库共用同一份语句。"""

CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_SQL = "\n".join(
    CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_STATEMENTS
)

CONTEXT_SOURCE_VERSION_SCHEMA_SQL = load_sql(__package__, "migrations/context_sources/context_source_versions.sql")
"""checkpoint 控制状态版本与未应用的真实 view rebuild 事实。"""


def ensure_query_indexes(connection: sqlite3.Connection) -> None:
    """新库、初始化与停写升级共用现行查询索引，不改变正文事实。"""
    for script in (
        ITEM_CATALOG_LOCATOR_INDEX_SQL, COMMIT_IDEMPOTENCY_INDEX_SQL,
        ITEM_CATALOG_MODEL_TOOL_INDEX_SQL, MODEL_CALLS_ASSEMBLY_INDEX_SQL,
        CONTEXT_CONTRIBUTIONS_SOURCE_REF_INDEX_SQL, CONTEXT_CONTRIBUTIONS_OVERLAY_INDEX_SQL,
        SOURCE_OVERLAY_LOCATOR_INDEXES_SQL, TOOL_CALLS_MESSAGE_INDEX_SQL,
        load_sql(__package__, "migrations/execution/item_catalog_background_result_identity.sql"),
    ):
        _execute_migration_sql(connection, script)


def initialize_rollout_schema(
    connection: sqlite3.Connection,
    *,
    initialize_activation: Callable[[sqlite3.Connection], None],
) -> None:
    """开启新库初始化事务；由调用方连同 owner metadata 一起提交。"""
    connection.executescript(
        "BEGIN IMMEDIATE;\n"
        + PLAN_REGISTRY_SCHEMA_SQL
        + TOOL_SET_SNAPSHOT_SCHEMA_SQL
        + CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_SQL
        + CONTEXT_SOURCE_VERSION_SCHEMA_SQL
        + RUNTIME_WAKEUP_SCHEMA_SQL
        + HISTORY_NAMESPACE_SCHEMA_SQL
        + ITEM_RESOURCE_REFERENCES_SCHEMA_SQL
        + ASSEMBLY_REF_SCHEMA_SQL
        + CHANNEL_VALUES_SCHEMA_SQL
        + MESSAGES_SCHEMA_SQL
        + load_sql(__package__, "migrations/rollout.sql")
        + VIEW_CREATION_SCHEMA_SQL
        + ASSEMBLY_CONTENT_SCHEMA_SQL
        + VIEW_MEMBERSHIP_SCHEMA_SQL
        + EXECUTION_QUEUE_SCHEMA_SQL
        + EXECUTION_SCHEDULING_MIGRATION_SQL
    )

    ensure_query_indexes(connection)
    initialize_activation(connection)


__all__ = [
    "CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_SQL",
    "CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_STATEMENTS",
    "MESSAGE_FORMAT_VERSION",
    "ROLLOUT_FORMAT_VERSION",
    "ROLLOUT_SCHEMA_VERSION",
    "initialize_rollout_schema",
]
