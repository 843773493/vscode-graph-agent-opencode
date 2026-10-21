"""将真实提交的现行状态还原成旧表，供显式迁移与损坏边界测试使用。"""

from __future__ import annotations

import hashlib
import sqlite3


def restore_schema19(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        ALTER TABLE checkpoint_channels RENAME TO checkpoint_channels_v20;
        DROP INDEX checkpoint_channels_value;
        CREATE TABLE checkpoint_channels (
            checkpoint_id TEXT NOT NULL, channel_name TEXT NOT NULL,
            storage_kind TEXT NOT NULL, value_state TEXT NOT NULL, channel_version TEXT,
            serializer_name TEXT, value_blob BLOB, value_length INTEGER, value_hash TEXT,
            context_view_id TEXT, updated_index INTEGER, created_at TEXT NOT NULL,
            PRIMARY KEY(checkpoint_id, channel_name)
        );
        INSERT INTO checkpoint_channels
        SELECT channel.checkpoint_id,channel.channel_name,channel.storage_kind,channel.value_state,
            channel.channel_version,value.serializer_name,value.value_blob,value.value_length,value.value_hash,
            channel.context_view_id,channel.updated_index,channel.created_at
        FROM checkpoint_channels_v20 channel LEFT JOIN checkpoint_state_values value ON value.value_id=channel.value_id;
        DROP TABLE checkpoint_channels_v20;
        DROP TABLE checkpoint_state_values;
        UPDATE database_meta SET schema_version=19;
    """)
    connection.execute(
        "UPDATE schema_migrations SET to_version=19,migration_name='rollout_sqlite_v19',migration_checksum=? WHERE from_version=0",
        (hashlib.sha256(b"rollout_sqlite_v19").hexdigest(),),
    )
    connection.commit()
