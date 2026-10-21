"""共享不可变成员块；logical/context 坐标原样保存，业务选择由既有 owner 决定。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from app.domain.itemized.hashing import sha256_jcs

Member = tuple[str, int, int, str, int, int | None]
COLUMNS = "item_id,logical_item_ordinal,visible,source_kind,context_selected,context_ordinal"
BLOCK_SIZE = 16


def read_members(connection: sqlite3.Connection, view_id: str) -> list[Member]:
    return [tuple(row) for row in connection.execute(
        f"SELECT {COLUMNS} FROM context_view_items WHERE view_id=? ORDER BY logical_item_ordinal,item_id",
        (view_id,),
    )]


def store_block(connection: sqlite3.Connection, members: list[Member]) -> int:
    """只存不可变有序块；view 级唯一性由发布入口整体检查。"""
    if not connection.in_transaction:
        raise RuntimeError("view members 写入必须位于原事务内")
    key = sha256_jcs([list(row) for row in members])
    existing = connection.execute(
        "SELECT block_id FROM context_membership_blocks WHERE content_hash=?", (key,),
    ).fetchone()
    if existing is not None:
        stored = [tuple(row) for row in connection.execute(
            f"SELECT {COLUMNS} FROM context_membership_items WHERE block_id=? ORDER BY member_index",
            (existing[0],),
        )]
        if stored != members:
            raise ValueError(f"source-mismatch: view member block 同版本异内容: {key}")
        return existing[0]
    block = connection.execute(
        "INSERT INTO context_membership_blocks(content_hash) VALUES (?)", (key,),
    ).lastrowid
    connection.executemany(
        f"INSERT INTO context_membership_items(block_id,member_index,{COLUMNS}) VALUES (?,?,?,?,?,?,?,?)",
        ((block, index, *row) for index, row in enumerate(members)),
    )
    return block


def replace_members(connection: sqlite3.Connection, view_id: str, members: Iterable[Member]) -> None:
    """在同一事务发布有序块引用；没有逐 view 的完整成员副本。"""
    if not connection.in_transaction:
        raise RuntimeError("view members 发布必须位于原事务内")
    if connection.execute("SELECT 1 FROM context_views WHERE view_id=?", (view_id,)).fetchone() is None:
        raise ValueError(f"source-mismatch: view 不存在: {view_id}")
    rows = sorted((tuple(row) for row in members), key=lambda row: (row[1], row[0]))
    for field in (0, 1, 5):
        values = [row[field] for row in rows if row[field] is not None]
        if len(values) != len(set(values)):
            raise sqlite3.IntegrityError(f"context_view_items 唯一坐标冲突: {view_id}/{field}")
    blocks = [store_block(connection, rows[index:index + BLOCK_SIZE]) for index in range(0, len(rows), BLOCK_SIZE)]
    connection.execute("DELETE FROM context_view_item_blocks WHERE view_id=?", (view_id,))
    connection.executemany(
        "INSERT INTO context_view_item_blocks(view_id,block_ordinal,block_id) VALUES (?,?,?)",
        ((view_id, index, block) for index, block in enumerate(blocks)),
    )


def append_members(connection: sqlite3.Connection, view_id: str, members: Iterable[Member]) -> None:
    replace_members(connection, view_id, [*read_members(connection, view_id), *members])


def clone_members(connection: sqlite3.Connection, source_view: str, target_view: str) -> None:
    if not connection.in_transaction:
        raise RuntimeError("view members 克隆必须位于原事务内")
    connection.execute(
        "INSERT INTO context_view_item_blocks(view_id,block_ordinal,block_id) "
        "SELECT ?,block_ordinal,block_id FROM context_view_item_blocks WHERE view_id=?",
        (target_view, source_view),
    )


def prune_members(connection: sqlite3.Connection) -> None:
    """fork 私有事务按已冻结 canonical 闭包裁剪各 view，不改共享块内容。"""
    views = connection.execute(
        "SELECT DISTINCT view_id FROM context_view_items WHERE item_id NOT IN (SELECT item_id FROM fork_selected_items)",
    ).fetchall()
    for (view_id,) in views:
        retained = connection.execute(
            f"SELECT {COLUMNS} FROM context_view_items WHERE view_id=? "
            "AND item_id IN (SELECT item_id FROM fork_selected_items) ORDER BY logical_item_ordinal,item_id",
            (view_id,),
        ).fetchall()
        replace_members(connection, view_id, retained)


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
