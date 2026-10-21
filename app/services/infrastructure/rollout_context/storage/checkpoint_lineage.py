"""Delta 恢复与 fork 共用的精确父链依赖。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence


def delta_recovery_channels(connection: sqlite3.Connection, checkpoint_id: str) -> tuple[str, ...]:
    """官方计数标识省略正文的Delta；已消费的EphemeralValue不依赖父链。"""
    return tuple(row[0] for row in connection.execute(
        "SELECT channel.channel_name FROM checkpoint_channels channel "
        "JOIN checkpoints checkpoint USING(checkpoint_id) WHERE checkpoint_id=? AND "
        "(channel.value_state='delta' OR (channel.value_state='absent' AND channel.channel_version IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM json_each(checkpoint.metadata_json,'$.counters_since_delta_snapshot') counter "
        "WHERE counter.key=channel.channel_name)))", (checkpoint_id,),
    ))


def delta_history_path(
    connection: sqlite3.Connection, checkpoint_id: str, *,
    checkpoint_ns: str, channels: Sequence[str],
) -> tuple[str, ...]:
    """返回目标之前、直到各通道基准的祖先；目标 pending 不在其中。"""
    remaining = set(channels)
    visited: set[str] = set()
    path: list[str] = []
    cursor: str | None = checkpoint_id
    while cursor is not None and remaining:
        if cursor in visited:
            raise RuntimeError(f"checkpoint parent lineage 存在循环: {cursor}")
        visited.add(cursor)
        checkpoint = connection.execute(
            "SELECT parent_checkpoint_id FROM checkpoints "
            "WHERE checkpoint_id=? AND checkpoint_ns=? AND status='active'",
            (cursor, checkpoint_ns),
        ).fetchone()
        if checkpoint is None:
            raise RuntimeError(f"checkpoint parent lineage 缺少已引用的祖先: {cursor}")
        rows = {row[0]: row[1:] for row in connection.execute(
            "SELECT channel_name,value_state,channel_version FROM checkpoint_channels WHERE checkpoint_id=?",
            (cursor,),
        )}
        if "messages" in remaining and "messages" not in rows:
            raise RuntimeError(f"checkpoint 缺少 messages channel: {cursor}")
        if cursor != checkpoint_id:
            path.append(cursor)
            remaining.difference_update(
                name for name, (state, _version) in rows.items()
                if state in ({"view", "snapshot"} if name == "messages" else {"present"})
            )
        if checkpoint[0] is None and "messages" in remaining:
            state, version = rows["messages"]
            if version is not None and state == "delta":
                raise RuntimeError(f"messages Delta 缺少基准且根 checkpoint 仍有消息版本: {cursor}")
        cursor = checkpoint[0]
    return tuple(path)
