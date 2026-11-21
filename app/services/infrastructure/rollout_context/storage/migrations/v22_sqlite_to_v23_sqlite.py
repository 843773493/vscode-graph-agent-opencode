"""rollout SQLite 22→23：在原迁移事务内将 view 成员改为共享块。"""

from __future__ import annotations

import sqlite3

from app.services.infrastructure.rollout_context.storage.catalog.view_items.store import (
    COLUMNS,
    read_members,
    replace_members,
)


def migrate_members(connection: sqlite3.Connection) -> None:
    """显式22→23事务内迁移并逐 view 核对六个字段，旧表最终由登记SQL删除。"""
    views = connection.execute("SELECT view_id FROM context_views ORDER BY view_id").fetchall()
    for (view_id,) in views:
        before = connection.execute(
            f"SELECT {COLUMNS} FROM context_view_items_legacy WHERE view_id=? ORDER BY logical_item_ordinal,item_id",
            (view_id,),
        ).fetchall()
        replace_members(connection, view_id, before)
        if read_members(connection, view_id) != [tuple(row) for row in before]:
            raise ValueError(f"source-mismatch: view members migration 改变历史坐标: {view_id}")
    if connection.execute(
        "SELECT 1 FROM context_view_items_legacy old LEFT JOIN context_views v ON v.view_id=old.view_id "
        "WHERE v.view_id IS NULL LIMIT 1",
    ).fetchone() is not None:
        raise ValueError("source-mismatch: view members migration 孤立view")
