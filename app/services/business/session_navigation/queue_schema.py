"""会话目录异步 mutation 队列两张旁挂表的物理 schema 与幂等建表。

- ``navigation_mutation_records``：typed 导航 operation 的 enqueue 事实、
  单调 ``queue_seq``、依赖、Folder ID 预留、terminal 状态与 compact tombstone。
- ``navigation_events``：独立 ``navigation`` channel 的终态事件 outbox，
  ``(workspace_id, event_seq)`` 唯一且单调。

这两张表是纯加法式旁挂：不注册进 ``SessionCatalogStore`` 的
``_REQUIRED_TABLES``、不改 ``user_version``、不触碰 ``nodes`` /
creation / subtree 权威表，因此与 8.1-F 的启动校验不冲突。本模块只承载物理
schema 与一次幂等建表，不含任何队列语义。
"""

from __future__ import annotations

from app.core.session_catalog_store import SessionCatalogStore
from app.core.sqlite_state import load_sql

__all__ = ["ensure_navigation_queue_tables"]

_MUTATION_RECORDS_DDL = load_sql(__package__, "sql/navigation_mutation_records.sql")

_MUTATION_RECORDS_QUEUE_DDL = load_sql(__package__, "sql/idx_navigation_mutation_queue.sql")
_MUTATION_RECORDS_PENDING_DDL = load_sql(__package__, "sql/idx_navigation_mutation_pending.sql")

_EVENTS_DDL = load_sql(__package__, "sql/navigation_events.sql")

_QUEUE_OWNERS_DDL = load_sql(__package__, "sql/navigation_queue_owners.sql")


def ensure_navigation_queue_tables(store: SessionCatalogStore) -> None:
    """幂等建旁挂表；已存在时不写事务（避免无谓推进 generation）。"""
    with store.read_transaction() as connection:
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    if {
        "navigation_mutation_records",
        "navigation_events",
        "navigation_queue_owners",
    } <= present:
        return
    with store.write_transaction() as connection:
        connection.execute(_MUTATION_RECORDS_DDL)
        connection.execute(_MUTATION_RECORDS_QUEUE_DDL)
        connection.execute(_MUTATION_RECORDS_PENDING_DDL)
        connection.execute(_EVENTS_DDL)
        connection.execute(_QUEUE_OWNERS_DDL)
