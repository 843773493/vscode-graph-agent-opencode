"""状态版本的现行结构与显式 19→20 迁移；不猜测历史 owner。"""

from __future__ import annotations

import sqlite3

from app.core.hashing import sha256_hex
from app.core.sqlite_state import load_sql
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    require_historical_scope,
)

CHANNEL_VALUES_SCHEMA_SQL = load_sql(__package__, "sql/checkpoint_state_values.sql") + load_sql(__package__, "sql/checkpoint_channels.sql")
CHANNEL_VALUES_MIGRATION_SQL = (
    load_sql(__package__, "sql/checkpoint_channels_019_020_prepare.sql")
    + CHANNEL_VALUES_SCHEMA_SQL
    + load_sql(__package__, "sql/checkpoint_channels_019_020.sql")
)


def validate_legacy_channel_values(connection: sqlite3.Connection) -> None:
    """只在升级事务内读取旧正文；碰撞、损坏与缺失来源先明确失败。"""
    versions = {}
    for row in connection.execute(
        "SELECT channel.checkpoint_id,channel.channel_name,channel.storage_kind,channel.value_state,"
        "channel.channel_version,channel.serializer_name,channel.value_blob,channel.value_length,"
        "channel.value_hash,channel.context_view_id,source.session_id,source.thread_id,"
        "source.checkpoint_ns,checkpoint.checkpoint_ns FROM checkpoint_channels channel "
        "LEFT JOIN checkpoint_context_source_snapshots source ON source.checkpoint_id=channel.checkpoint_id "
        "LEFT JOIN checkpoints checkpoint ON checkpoint.checkpoint_id=channel.checkpoint_id"
    ):
        checkpoint, channel, kind, state, version, serializer, blob, length, digest, view = row[:10]
        if state == "view" and channel == "messages" and kind == "rollout_view" and view:
            if any(value is not None for value in (serializer, blob, length, digest)):
                raise RuntimeError(f"schema migration messages 正文状态非法: {checkpoint}")
            continue
        if kind == "sqlite_value" and state == "absent":
            if any(value is not None for value in (serializer, blob, length, digest, view)):
                raise RuntimeError(f"schema migration absent channel 状态非法: {checkpoint}/{channel}")
            continue
        if (kind != "sqlite_value" or state != "present" or view is not None
                or not isinstance(version, str) or not version or not isinstance(serializer, str) or not serializer
                or not isinstance(blob, bytes) or type(length) is not int or length != len(blob)
                or sha256_hex(blob) != digest):
            raise RuntimeError(f"schema migration channel BLOB 校验失败: {checkpoint}/{channel}")
        if any(not isinstance(value, str) or not value for value in row[10:12]) or row[12] != row[13] or row[12] is None:
            raise RuntimeError(f"schema migration 缺少确切 checkpoint owner/namespace: {checkpoint}/{channel}")
        require_historical_scope(connection, row[10], row[11])
        key = (*row[10:13], channel, version)
        content = (serializer, length, digest)
        if key in versions and versions[key] != content:
            raise ValueError(f"schema migration channel 同版本异内容: {key!r}, checkpoint={checkpoint}")
        versions[key] = content
