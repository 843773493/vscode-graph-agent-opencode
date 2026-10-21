"""将真实提交的现行状态还原成旧表，供显式迁移与损坏边界测试使用。"""

from __future__ import annotations

import hashlib
import sqlite3

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
)


def restore_schema22(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT * FROM context_view_items").fetchall()
    connection.executescript("""
        DROP VIEW context_view_items;
        CREATE TABLE context_view_items (
            view_id TEXT NOT NULL, item_id TEXT NOT NULL, logical_item_ordinal INTEGER NOT NULL,
            visible INTEGER NOT NULL DEFAULT 1, source_kind TEXT NOT NULL DEFAULT 'canonical',
            context_selected INTEGER NOT NULL DEFAULT 1 CHECK(context_selected IN (0,1)), context_ordinal INTEGER,
            PRIMARY KEY(view_id,item_id), UNIQUE(view_id,logical_item_ordinal), UNIQUE(view_id,context_ordinal)
        );
        CREATE INDEX context_view_items_ordinal_index ON context_view_items(view_id,logical_item_ordinal);
        DROP TABLE context_view_item_blocks;
        DROP TABLE context_membership_items;
        DROP TABLE context_membership_blocks;
        UPDATE database_meta SET schema_version=22;
    """)
    connection.executemany("INSERT INTO context_view_items VALUES (?,?,?,?,?,?,?)", rows)
    connection.execute("UPDATE schema_migrations SET to_version=22,migration_name='rollout_sqlite_v22',migration_checksum=? WHERE from_version=0", (hashlib.sha256(b"rollout_sqlite_v22").hexdigest(),))
    connection.commit()


def restore_schema21(connection: sqlite3.Connection) -> None:
    restore_schema22(connection)
    for key, raw in connection.execute("SELECT assembly_id,snapshot_json FROM context_assemblies").fetchall():
        connection.execute("UPDATE context_assemblies SET snapshot_json=? WHERE assembly_id=?", (canonical_json_bytes(load_snapshot(connection, raw)).decode(), key))
    connection.execute("DROP TABLE assembly_content_nodes")
    connection.execute("UPDATE database_meta SET schema_version=21")
    connection.execute("UPDATE schema_migrations SET to_version=21,migration_name='rollout_sqlite_v21',migration_checksum=? WHERE from_version=0", (hashlib.sha256(b"rollout_sqlite_v21").hexdigest(),))
    connection.commit()


def restore_schema20(connection: sqlite3.Connection) -> None:
    restore_schema21(connection)
    connection.executescript("""
        ALTER TABLE messages RENAME TO messages_v21;
        DROP INDEX messages_turn_index;
        DROP INDEX messages_role_index;
        DROP INDEX messages_offset_index;
        DROP INDEX messages_execution_index;
        DROP INDEX messages_logical_identity;
        CREATE TABLE messages (
            message_sequence INTEGER PRIMARY KEY, message_id TEXT NOT NULL UNIQUE,
            turn_id TEXT, execution_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
            jsonl_offset INTEGER NOT NULL, jsonl_length INTEGER NOT NULL,
            content_length INTEGER NOT NULL, content_hash TEXT NOT NULL,
            visibility TEXT NOT NULL, commit_id INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        INSERT INTO messages SELECT * FROM messages_v21;
        DROP TABLE messages_v21;
        CREATE INDEX messages_turn_index ON messages(turn_id, message_sequence);
        CREATE INDEX messages_role_index ON messages(role, message_sequence);
        CREATE INDEX messages_offset_index ON messages(jsonl_offset);
        CREATE INDEX messages_execution_index ON messages(execution_id, message_sequence);
        UPDATE database_meta SET schema_version=20;
    """)
    connection.execute(
        "UPDATE schema_migrations SET to_version=20,migration_name='rollout_sqlite_v20',migration_checksum=? WHERE from_version=0",
        (hashlib.sha256(b"rollout_sqlite_v20").hexdigest(),),
    )
    connection.commit()


def restore_schema19(connection: sqlite3.Connection) -> None:
    restore_schema20(connection)
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
