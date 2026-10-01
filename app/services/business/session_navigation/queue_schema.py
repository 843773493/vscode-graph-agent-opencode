"""会话目录异步 mutation 队列两张旁挂表的物理 schema 与幂等建表。

- ``navigation_mutation_records``：typed 导航 operation 的 enqueue 事实、
  单调 ``queue_seq``、依赖、Folder ID 预留、terminal 状态与 compact tombstone。
- ``navigation_events``：独立 ``navigation`` channel 的终态事件 outbox，
  ``(workspace_id, event_seq)`` 唯一且单调。

这两张表是纯加法式旁挂：不注册进 ``SessionCatalogStore`` 的
``_REQUIRED_TABLES_BY_VERSION``、不改 ``user_version``、不触碰 ``nodes`` /
creation / subtree 权威表，因此与 8.1-F 的启动校验不冲突。本模块只承载物理
schema 与一次幂等建表，不含任何队列语义。
"""

from __future__ import annotations

from app.core.session_catalog_store import SessionCatalogStore

__all__ = ["ensure_navigation_queue_tables"]

_MUTATION_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS navigation_mutation_records (
    gateway_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    client_sequence INTEGER NOT NULL,
    queue_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'committed', 'rejected', 'cancelled',
                  'dependency_failed')
    ),
    params_json TEXT NOT NULL,
    preimage_hash TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    created_by_operation_id TEXT,
    target_node_id TEXT,
    reserved_node_id TEXT,
    result_node_id TEXT,
    result_node_revision INTEGER,
    committed_catalog_revision INTEGER,
    error_code TEXT,
    error_detail TEXT,
    pending_settlement INTEGER NOT NULL DEFAULT 0,
    holder_id TEXT,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    receipt_revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (gateway_id, workspace_id, actor, operation_id)
)
"""

_MUTATION_RECORDS_QUEUE_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_navigation_mutation_queue "
    "ON navigation_mutation_records(workspace_id, queue_seq)"
)
_MUTATION_RECORDS_PENDING_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_navigation_mutation_pending "
    "ON navigation_mutation_records(workspace_id, state, queue_seq)"
)

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS navigation_events (
    workspace_id TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    queue_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    result_state TEXT NOT NULL,
    committed_catalog_revision INTEGER,
    affected_node_ids_json TEXT NOT NULL,
    error_code TEXT,
    error_detail TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, event_seq)
)
"""


def ensure_navigation_queue_tables(store: SessionCatalogStore) -> None:
    """幂等建旁挂表；已存在时不写事务（避免无谓推进 generation）。"""
    with store.read_transaction() as connection:
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    if {"navigation_mutation_records", "navigation_events"} <= present:
        return
    with store.write_transaction() as connection:
        connection.execute(_MUTATION_RECORDS_DDL)
        connection.execute(_MUTATION_RECORDS_QUEUE_DDL)
        connection.execute(_MUTATION_RECORDS_PENDING_DDL)
        connection.execute(_EVENTS_DDL)
