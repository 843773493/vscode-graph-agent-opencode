"""局部索引装具与损坏注入；不替代产品执行/fork入口，也不改写其它view的共享块。"""

from __future__ import annotations

import sqlite3

from app.services.infrastructure.rollout_context.storage.catalog.view_items.store import (
    BLOCK_SIZE,
    COLUMNS,
    store_block,
)


def mutate_view_members(
    connection: sqlite3.Connection, statement: str, parameters: tuple[object, ...] = (),
) -> sqlite3.Cursor:
    """先对测试用临时关系执行原SQL，再以私有块注入结果；允许故意损坏唯一坐标。"""
    if "context_view_items" not in statement:
        return connection.execute(statement, parameters)
    connection.execute("CREATE TEMP TABLE injected_view_members AS SELECT * FROM context_view_items")
    try:
        result = connection.execute(statement.replace("context_view_items", "injected_view_members"), parameters)
        views = connection.execute(
            "SELECT view_id FROM context_view_item_blocks UNION SELECT view_id FROM injected_view_members",
        ).fetchall()
        for (view_id,) in views:
            members = [tuple(row) for row in connection.execute(
                f"SELECT {COLUMNS} FROM injected_view_members WHERE view_id=? ORDER BY logical_item_ordinal,item_id",
                (view_id,),
            )]
            connection.execute("DELETE FROM context_view_item_blocks WHERE view_id=?", (view_id,))
            for index in range(0, len(members), BLOCK_SIZE):
                block = store_block(connection, members[index:index + BLOCK_SIZE])
                connection.execute(
                    "INSERT INTO context_view_item_blocks VALUES (?,?,?)", (view_id, index // BLOCK_SIZE, block),
                )
    finally:
        connection.execute("DROP TABLE injected_view_members")
    return result


def mutate_view_members_many(connection: sqlite3.Connection, statement: str, parameters: list[tuple[object, ...]]) -> None:
    for values in parameters:
        mutate_view_members(connection, statement, values)
