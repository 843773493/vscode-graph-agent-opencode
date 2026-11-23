"""真实消息写入后的 v29→v30 补算、备份与失败回滚。"""

import hashlib
import sqlite3
from contextlib import closing

import pytest

from app.services.infrastructure.rollout_context.storage.schema import (
    RESPONSE_OUTLINE_MIGRATION_SQL,
)
from tests.integration.backend.sessions.rollout_context.test_message_revisions import (
    revision_case as revision_case,  # noqa: PLC0414 - 复用正式准入及消息提交 fixture
)
from tests.integration.backend.sessions.rollout_context.test_message_revisions import (
    start_model_call as start_model_call,  # noqa: PLC0414
)


@pytest.fixture
def schema29_case(revision_case):
    saver, admission, index, *_ = revision_case
    with closing(sqlite3.connect(index)) as connection, connection:
        expected = connection.execute("SELECT * FROM message_projections ORDER BY message_sequence").fetchall()
        messages = connection.execute("SELECT * FROM messages ORDER BY message_sequence").fetchall()
        # v29 与 v30 的唯一结构差异；正文由上面的真实 Saver 链路提交。
        connection.execute("ALTER TABLE message_projections DROP COLUMN response_outline_json")
        connection.execute("UPDATE database_meta SET schema_version=29")
        connection.execute("UPDATE schema_migrations SET to_version=29,migration_name='rollout_sqlite_v29',migration_checksum=? WHERE from_version=0",
            (hashlib.sha256(b"rollout_sqlite_v29").hexdigest(),))
    return saver._storage.storage, admission, index, expected, messages, index.with_name("rollout.jsonl").read_bytes()


def test_upgrade_backfills_real_outlines_and_keeps_current_startup_idempotent(schema29_case):
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

    monkeypatch.setattr(storage._codec(), "activity_outline", fail_outline)
    with pytest.raises(RuntimeError, match="测试轮廓失败"):
        storage.migrate_schema(admission.thread_id, session_id=admission.session_id, to_version=30,
            migration_name="rollout_sqlite_v30", migration_sql=RESPONSE_OUTLINE_MIGRATION_SQL)
    with closing(sqlite3.connect(index)) as connection:
        assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (29, "recovery_required")
        assert "response_outline_json" not in {row[1] for row in connection.execute("PRAGMA table_info(message_projections)")}
        assert connection.execute("SELECT * FROM messages ORDER BY message_sequence").fetchall() == messages
        assert connection.execute("SELECT status FROM schema_migrations WHERE from_version=29").fetchall() == [("failed",)]
    assert index.with_name("rollout.jsonl").read_bytes() == canonical
