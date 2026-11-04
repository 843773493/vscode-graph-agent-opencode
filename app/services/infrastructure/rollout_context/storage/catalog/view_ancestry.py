"""context view 的父链读取与 jump 写入；只消费指定 view 的祖先。"""

from __future__ import annotations

import sqlite3

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_optional_text,
    strict_text,
)


def read_view_ancestry(connection: sqlite3.Connection, view_id: str) -> list[str]:
    """沿主键读取一次父链；UNION 让成环输入收敛，再明确报告环或缺失父节点。"""
    rows = connection.execute(
        "WITH RECURSIVE ancestry(view_id,parent_view_id) AS ("
        "SELECT view_id,parent_view_id FROM context_views WHERE view_id=? "
        "UNION "
        "SELECT parent.view_id,parent.parent_view_id FROM context_views parent "
        "JOIN ancestry child ON parent.view_id=child.parent_view_id) "
        "SELECT view_id,parent_view_id FROM ancestry",
        (view_id,),
    )
    parents = {
        strict_text(row[0], field="context_views.view_id"): strict_optional_text(
            row[1], field="context_views.parent_view_id",
        )
        for row in rows
    }
    if view_id not in parents:
        raise RuntimeError(f"context view 不存在: {view_id}")
    chain: list[str] = []
    visited: set[str] = set()
    current: str | None = view_id
    while current is not None:
        if current in visited:
            raise RuntimeError(f"context view 父链成环: {view_id}")
        visited.add(current)
        chain.append(current)
        parent = parents[current]
        if parent is not None and parent not in parents:
            raise RuntimeError(f"context view 引用不存在的 parent: {current} -> {parent}")
        current = parent
    return chain


def insert_view_jumps(connection: sqlite3.Connection, view_id: str) -> None:
    """共享一次已校验的父链，按深度取祖先；不为每个 level 重走父指针。"""
    chain = read_view_ancestry(connection, view_id)
    for level in range((len(chain) - 1).bit_length()):
        depth = 1 << level
        inserted = connection.execute(
            "INSERT INTO context_view_jumps(view_id,jump_level,ancestor_view_id,ancestor_depth) VALUES (?,?,?,?)",
            (view_id, level, chain[depth], depth),
        )
        if inserted.rowcount != 1:
            raise RuntimeError(f"context view ancestor jump 插入失败: {view_id}/{level}")
