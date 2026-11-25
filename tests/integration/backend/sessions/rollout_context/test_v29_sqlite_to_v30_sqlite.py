"""真实消息写入后的 v29→v30 补算、备份与失败回滚。"""

import hashlib
import sqlite3
from contextlib import closing

import pytest

from app.services.infrastructure.rollout_context.storage.schema import (
    RESPONSE_OUTLINE_MIGRATION_SQL,
)
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_text,
)
from tests.integration.backend.sessions.rollout_context.test_message_revisions import (
    revision_case as revision_case,  # noqa: PLC0414 - 复用正式准入及消息提交 fixture
)
from tests.integration.backend.sessions.rollout_context.test_message_revisions import (
    start_model_call as start_model_call,  # noqa: PLC0414
)


@pytest.fixture
def schema29_case(revision_case):
    saver, admission, index, source, *_ = revision_case
    with closing(sqlite3.connect(index)) as connection, connection:
        # 当前31已移除轮廓；这里显式恢复被测29的消息投影版本，不走生产自动降级。
        connection.execute("UPDATE message_projections SET projection_version=2")
        projection_rows = connection.execute("SELECT * FROM message_projections ORDER BY message_sequence").fetchall()
        outline = {source.item_id: [{"kind": "text", "part_ordinal": 0,
            "content_block_index": None, "item_index": None, "carrier_type": None,
            "call_index": None, "tool_call_id": None, "tool_name": None,
            "text": "原始正文", "truncated": False}]}
        expected = [(*row[:3], canonical_json_text(outline if row[8] == "assistant_text" else {}), *row[3:])
                    for row in projection_rows]
        messages = connection.execute("SELECT * FROM messages ORDER BY message_sequence").fetchall()
        connection.execute("UPDATE database_meta SET schema_version=29")
        connection.execute("UPDATE schema_migrations SET to_version=29,migration_name='rollout_sqlite_v29',migration_checksum=? WHERE from_version=0",
            (hashlib.sha256(b"rollout_sqlite_v29").hexdigest(),))
    return saver._storage.storage, admission, index, expected, messages, index.with_name("rollout.jsonl").read_bytes()


def test_upgrade_backfills_frozen_v30_outlines_without_running_v31_migration(schema29_case):
    storage, admission, index, expected, messages, canonical = schema29_case
    storage.migrate_schema(admission.thread_id, session_id=admission.session_id, to_version=30,
        migration_name="rollout_sqlite_v30", migration_sql=RESPONSE_OUTLINE_MIGRATION_SQL)
    with closing(sqlite3.connect(index)) as connection:
        assert connection.execute("SELECT * FROM message_projections ORDER BY message_sequence").fetchall() == expected
        assert connection.execute("SELECT * FROM messages ORDER BY message_sequence").fetchall() == messages
        assert connection.execute("SELECT schema_version FROM database_meta").fetchone() == (30,)
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='message_projections_v29'").fetchall()
        column = next(row for row in connection.execute("PRAGMA table_info(message_projections)") if row[1] == "response_outline_json")
        assert column[3:5] == (1, None)  # 与新库一致：必填且无虚假默认值。
    assert any(row[3] != "{}" for row in expected)
    backups = tuple(index.parent.glob("index.sqlite.migration-*.backup"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as backup:
        assert backup.execute("SELECT schema_version FROM database_meta").fetchone() == (29,)
        assert backup.execute("SELECT * FROM messages ORDER BY message_sequence").fetchall() == messages
    # 29→30授权不隐含30→31；新版启动不能悄悄重建旧库或改版本号。
    with pytest.raises(RuntimeError, match="schema-incompatible"):
        storage.initialize(admission.session_id, thread_id=admission.thread_id)
    assert tuple(index.parent.glob("index.sqlite.migration-*.backup")) == backups
    with pytest.raises(ValueError, match="按版本顺序"):
        storage.migrate_schema(admission.thread_id, session_id=admission.session_id, to_version=30,
            migration_name="rollout_sqlite_v30", migration_sql=RESPONSE_OUTLINE_MIGRATION_SQL)
    assert index.with_name("rollout.jsonl").read_bytes() == canonical


def test_outline_failure_restores_source_schema_and_preserves_canonical(schema29_case, monkeypatch):
    storage, admission, index, _expected, messages, canonical = schema29_case

    def fail_outline(_items):
        raise RuntimeError("测试轮廓失败")

    monkeypatch.setattr(
        "app.services.infrastructure.rollout_context.storage.migrations.v29_sqlite_to_v30_sqlite.response_outlines_v30",
        fail_outline,
    )
    with pytest.raises(RuntimeError, match="测试轮廓失败"):
        storage.migrate_schema(admission.thread_id, session_id=admission.session_id, to_version=30,
            migration_name="rollout_sqlite_v30", migration_sql=RESPONSE_OUTLINE_MIGRATION_SQL)
    with closing(sqlite3.connect(index)) as connection:
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (29, "recovery_required")
        assert "response_outline_json" not in {row[1] for row in connection.execute("PRAGMA table_info(message_projections)")}
        assert connection.execute("SELECT * FROM messages ORDER BY message_sequence").fetchall() == messages
        assert connection.execute("SELECT status FROM schema_migrations WHERE from_version=29").fetchall() == [("failed",)]
    assert index.with_name("rollout.jsonl").read_bytes() == canonical
