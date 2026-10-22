"""rollout v2 SQLite schema 的唯一建表 owner。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from app.core.sqlite_state import load_sql
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

ROLLOUT_SCHEMA_VERSION = 24
EXECUTION_QUEUE_SCHEMA_SQL = load_sql(__package__, "sql/execution_queue.sql")

MESSAGE_FORMAT_VERSION = 1
ROLLOUT_FORMAT_VERSION = 2

ASSEMBLY_CONTENT_SCHEMA_SQL = load_sql(__package__, "sql/assembly_content.sql")
VIEW_MEMBERSHIP_SCHEMA_SQL = load_sql(__package__, "sql/view_membership.sql")
VIEW_MEMBERSHIP_MIGRATION_SQL = load_sql(__package__, "sql/view_membership_022_023_prepare.sql") + VIEW_MEMBERSHIP_SCHEMA_SQL

MESSAGES_SCHEMA_SQL = load_sql(__package__, "sql/messages.sql")
MESSAGES_VERSION_MIGRATION_SQL = (
    load_sql(__package__, "sql/messages_020_021_prepare.sql")
    + MESSAGES_SCHEMA_SQL
    + load_sql(__package__, "sql/messages_020_021.sql")
)

HISTORY_NAMESPACE_SCHEMA_SQL = load_sql(__package__, "sql/history_namespace.sql")
"""复制历史的冻结 namespace；正文与历史业务 hash 不因当前归属改变。"""

ITEM_RESOURCE_REFERENCES_SCHEMA_SQL = load_sql(__package__, "sql/item_resource_references.sql")
"""canonical item 的 typed 引用索引；资源生命周期仍由各资源 owner 管理。"""

CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_STATEMENTS: tuple[str, ...] = (
    load_sql(__package__, "sql/context_source_control_states.sql"),
    load_sql(__package__, "sql/context_source_control_states_owner_index.sql"),
    load_sql(__package__, "sql/team_source_observations.sql"),
)
"""CSM 控制状态的唯一 DDL 定义；新库与既有 v4 库共用同一份语句。"""

CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_SQL = "\n".join(
    CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_STATEMENTS
)

CONTEXT_SOURCE_VERSION_SCHEMA_SQL = load_sql(__package__, "sql/context_source_versions.sql")
"""checkpoint 控制状态版本与未应用的真实 view rebuild 事实。"""


def initialize_rollout_schema(
    connection: sqlite3.Connection,
    *,
    initialize_activation: Callable[[sqlite3.Connection], None],
) -> None:
    """创建 v2 schema；不执行数据迁移或业务查询。"""
    connection.executescript(
        PLAN_REGISTRY_SCHEMA_SQL
        + TOOL_SET_SNAPSHOT_SCHEMA_SQL
        + CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_SQL
        + CONTEXT_SOURCE_VERSION_SCHEMA_SQL
        + RUNTIME_WAKEUP_SCHEMA_SQL
        + HISTORY_NAMESPACE_SCHEMA_SQL
        + ITEM_RESOURCE_REFERENCES_SCHEMA_SQL
        + ASSEMBLY_REF_SCHEMA_SQL
        + CHANNEL_VALUES_SCHEMA_SQL
        + MESSAGES_SCHEMA_SQL
        + load_sql(__package__, "sql/rollout.sql")
        + ASSEMBLY_CONTENT_SCHEMA_SQL
        + VIEW_MEMBERSHIP_SCHEMA_SQL
        + EXECUTION_QUEUE_SCHEMA_SQL
    )

    # 邮箱冷恢复按真实 ModelCall/ToolCall 定位，不能扫描全部已提交工具结果。
    connection.execute(
        load_sql(__package__, "sql/item_catalog_background_result_identity.sql")
    )

    initialize_activation(connection)


__all__ = [
    "CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_SQL",
    "CONTEXT_SOURCE_CONTROL_STATE_SCHEMA_STATEMENTS",
    "MESSAGE_FORMAT_VERSION",
    "ROLLOUT_FORMAT_VERSION",
    "ROLLOUT_SCHEMA_VERSION",
    "initialize_rollout_schema",
]
