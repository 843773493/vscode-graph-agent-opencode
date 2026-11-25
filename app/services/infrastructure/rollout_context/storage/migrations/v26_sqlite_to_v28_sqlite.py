"""固定来源 bf0f2e1d 的 rollout v26 候选沿原 owner 顺序升到 v28。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.core.identifier import create_uuid_hex
from app.core.sqlite_state import utc_now_text
from app.services.infrastructure.rollout_context.storage.rollout_maintenance_owner import (
    migrate_schema_snapshot,
)
from app.services.infrastructure.rollout_context.storage.rollout_schema_introspection import (
    RolloutSchemaIntrospectionMixin,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    EXECUTION_SCHEDULING_MIGRATION_SQL,
    TEAM_OBSERVATIONS_MIGRATION_SQL,
    ensure_query_indexes,
)


def validate_rollout_v26(connection: sqlite3.Connection, jsonl_path: Path, *, session_id: str, thread_id: str) -> None:
    owner = connection.execute("SELECT session_id,thread_id,schema_version FROM database_meta WHERE singleton_id=1").fetchone()
    if tuple(owner or ()) != (session_id, thread_id, 26):
        raise ValueError("rollout 转换来源必须是原 Thread 的 v26，保留原件")
    RolloutSchemaIntrospectionMixin._validate_schema_state(connection, expected_version=26)
    RolloutSchemaIntrospectionMixin._validate_commit_offsets(connection, jsonl_path)
    if connection.execute("SELECT 1 FROM execution_queue WHERE phase<>'terminal' LIMIT 1").fetchone():
        raise ValueError("rollout v26 执行队列未排空，须由原运行时先结算")


def upgrade_rollout_v26(connection: sqlite3.Connection, jsonl_path: Path, *, session_id: str, thread_id: str) -> None:
    validate_rollout_v26(connection, jsonl_path, session_id=session_id, thread_id=thread_id)
    connection.execute("BEGIN IMMEDIATE")
    with connection:
        for target, script in ((27, EXECUTION_SCHEDULING_MIGRATION_SQL), (28, TEAM_OBSERVATIONS_MIGRATION_SQL)):
            migrate_schema_snapshot(
                connection, current_version=target - 1, to_version=target,
                migration_name=f"rollout_sqlite_v{target}", migration_sql=script, jsonl_path=jsonl_path,
                transaction_id=create_uuid_hex(), timestamp=utc_now_text(),
            )
        ensure_query_indexes(connection)
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("rollout 转换后的完整性校验失败")
