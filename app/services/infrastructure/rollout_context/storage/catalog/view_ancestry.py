"""context view 的父链读取与 jump 写入；只消费指定 view 的祖先。"""

from __future__ import annotations

import sqlite3

from app.services.infrastructure.rollout_context.storage.transaction import (
    strict_non_negative_int,
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


def insert_view_jumps(connection: sqlite3.Connection, view_id: str, parent_view_id: str | None) -> None:
    """复用已发布的二倍祖先索引，生成新 view 的 O(log 深度) 跳转。"""
    ancestor = parent_view_id
    visited = {view_id}
    level = 0
    # 已走 2^level 步后，再从该祖先走同样步数；只校验本次消费的跳转，
    # 完整父链的环、缺失和历史跳转校验仍留给显式审计。
    while ancestor is not None:
        if ancestor in visited:
            raise RuntimeError(f"context view 父链成环: {view_id}")
        visited.add(ancestor)
        row = connection.execute(
            "SELECT source.parent_view_id,jump.ancestor_view_id,jump.ancestor_depth "
            "FROM context_views source LEFT JOIN context_view_jumps jump "
            "ON jump.view_id=source.view_id AND jump.jump_level=? WHERE source.view_id=?",
            (level, ancestor),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"context view 引用不存在的 parent: {view_id} -> {ancestor}")
        depth = 1 << level
        following = strict_optional_text(row[1], field="context_view_jumps.ancestor_view_id")
        if (following is not None and strict_non_negative_int(row[2], field="context_view_jumps.ancestor_depth") != depth
                or level == 0 and following != strict_optional_text(row[0], field="context_views.parent_view_id")):
            raise RuntimeError(f"context view jump 非法: view={ancestor}, level={level}")
        inserted = connection.execute(
            "INSERT INTO context_view_jumps(view_id,jump_level,ancestor_view_id,ancestor_depth) VALUES (?,?,?,?)",
            (view_id, level, ancestor, depth),
        )
        if inserted.rowcount != 1:
            raise RuntimeError(f"context view ancestor jump 插入失败: {view_id}/{level}")
        ancestor = following
        level += 1
