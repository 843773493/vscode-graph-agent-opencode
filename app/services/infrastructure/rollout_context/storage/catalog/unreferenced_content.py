"""仅回收无引用不可变内容；所有历史checkpoint/view/assembly仍是保留根。"""

from __future__ import annotations

import sqlite3

from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.assembly.content_store import (
    snapshot_node_references,
)
from app.services.infrastructure.rollout_context.storage.catalog.view_items.store import (
    COLUMNS,
)
from app.services.infrastructure.rollout_context.storage.checkpoint_lineage import (
    delta_history_path,
    delta_recovery_channels,
)
from app.services.infrastructure.rollout_context.storage.checkpoint_state_values import (
    read_state_value,
)


def collect_unreferenced_content(connection: sqlite3.Connection) -> dict[str, int]:
    """先核完整依赖闭包，再在调用方原事务内删除；不改历史期限或增量基准。"""
    if not connection.in_transaction:
        raise RuntimeError("不可变内容回收必须位于原事务内")
    if connection.execute(
        "SELECT 1 FROM checkpoint_channels channel LEFT JOIN checkpoints checkpoint USING(checkpoint_id) "
        "WHERE checkpoint.checkpoint_id IS NULL LIMIT 1",
    ).fetchone() is not None:
        raise ValueError("source-mismatch: 回收前发现checkpoint channel悬空引用")
    for value_id, session, thread, namespace, channel, version in connection.execute(
        "SELECT channel.value_id,channel.value_session_id,channel.value_thread_id,checkpoint.checkpoint_ns,"
        "channel.channel_name,channel.channel_version FROM checkpoint_channels channel "
        "JOIN checkpoints checkpoint USING(checkpoint_id) WHERE channel.value_id IS NOT NULL",
    ):
        read_state_value(connection, value_id, channel=channel, version=version,
                         checkpoint_ns=namespace, session_id=session, thread_id=thread)
    for checkpoint_id, namespace in connection.execute(
        "SELECT checkpoint_id,checkpoint_ns FROM checkpoints",
    ):
        delta_history_path(connection, checkpoint_id, checkpoint_ns=namespace,
                           channels=delta_recovery_channels(connection, checkpoint_id))
    nodes: set[tuple[str, str, str]] = set()
    for session, thread, raw in connection.execute("SELECT session_id,thread_id,snapshot_json FROM context_assemblies"):
        nodes.update(snapshot_node_references(connection, raw, owner=(session, thread)))
    if connection.execute(
        "SELECT 1 FROM context_view_item_blocks refs "
        "LEFT JOIN context_views view ON view.view_id=refs.view_id "
        "LEFT JOIN context_membership_blocks block ON block.block_id=refs.block_id "
        "WHERE view.view_id IS NULL OR block.block_id IS NULL LIMIT 1",
    ).fetchone() is not None:
        raise ValueError("source-mismatch: 回收前发现view成员块悬空引用")
    for block, digest in connection.execute(
        "SELECT block_id,content_hash FROM context_membership_blocks "
        "WHERE block_id IN (SELECT block_id FROM context_view_item_blocks)",
    ):
        rows = connection.execute(
            f"SELECT member_index,{COLUMNS} FROM context_membership_items WHERE block_id=? ORDER BY member_index",
            (block,),
        ).fetchall()
        if (not rows or [row[0] for row in rows] != list(range(len(rows)))
                or sha256_jcs([list(row[1:]) for row in rows]) != digest):
            raise ValueError(f"source-mismatch: 回收前view成员块hash不一致: {block}")
    unreferenced = [tuple(row) for row in connection.execute(
        "SELECT session_id,thread_id,node_id FROM assembly_content_nodes",
    ) if tuple(row) not in nodes]
    # 到此所有保留根均已通过核验；以下删除由原事务一起提交或回滚。
    states = connection.execute(
        "DELETE FROM checkpoint_state_values WHERE value_id NOT IN "
        "(SELECT value_id FROM checkpoint_channels WHERE value_id IS NOT NULL)",
    ).rowcount
    connection.executemany(
        "DELETE FROM assembly_content_nodes WHERE session_id=? AND thread_id=? AND node_id=?", unreferenced,
    )
    members = connection.execute(
        "DELETE FROM context_membership_items WHERE block_id NOT IN (SELECT block_id FROM context_view_item_blocks)",
    ).rowcount
    blocks = connection.execute(
        "DELETE FROM context_membership_blocks WHERE block_id NOT IN (SELECT block_id FROM context_view_item_blocks)",
    ).rowcount
    return {"state_values": states, "assembly_nodes": len(unreferenced), "membership_items": members, "membership_blocks": blocks}
