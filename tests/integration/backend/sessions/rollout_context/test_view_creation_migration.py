"""view 创建顺序的显式25→26升级及原子回滚。"""

from __future__ import annotations

import errno
import json
import logging
import sqlite3
from contextlib import closing

import pytest

from app.core.sqlite_state import sqlite_file_uri
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.storage import (
    rollout_maintenance_owner,
)
from app.services.infrastructure.rollout_context.storage.schema import (
    VIEW_CREATION_MIGRATION_SQL,
)
from tests.integration.backend.sessions.rollout_context._channel_version_fixtures import (
    restore_schema25,
)
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_case as epoch_case,  # noqa: PLC0414 - 使用真实准入、封存和checkpoint
)
from tests.integration.backend.sessions.rollout_context.test_context_epoch_transition import (
    epoch_db as epoch_db,  # noqa: PLC0414 - 使用当前测试文件的隔离工作区
)


@pytest.mark.parametrize("interrupted", [False, True])
def test_schema25_view_order_upgrade_preserves_history_or_rolls_back(epoch_case, monkeypatch, interrupted):
    case = epoch_case
    case.load_snapshot()
    assembly = case.snapshot(case.prepare("before-view-order-migration"))
    case.checkpoint("view-order-checkpoint")
    original_assembly = canonical_json_bytes(assembly.to_dict())
    storage = case.saver._storage.storage
    jsonl = storage.jsonl_path(case.owner.session_id, thread_id=case.owner.thread_id)
    original_jsonl = jsonl.read_bytes()
    restore_schema25(case.connection)
    original_views = case.connection.execute("SELECT rowid,* FROM context_views ORDER BY rowid").fetchall()
    expected_order = [(row[1], row[0]) for row in original_views]
    original_migrate = rollout_maintenance_owner.execute_atomic_schema_sql

    if interrupted:
        def fail_after_ddl(connection, script):
            original_migrate(connection, script)
            raise RuntimeError("view创建顺序迁移发布前中断")

        monkeypatch.setattr(rollout_maintenance_owner, "execute_atomic_schema_sql", fail_after_ddl)
        with pytest.raises(RuntimeError, match="发布前中断"):
            storage.migrate_schema(
                case.owner.thread_id, session_id=case.owner.session_id, to_version=26,
                migration_name="view_creation_025_026", migration_sql=VIEW_CREATION_MIGRATION_SQL,
            )
        assert case.connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (25, "recovery_required")
        assert case.connection.execute("SELECT rowid,* FROM context_views ORDER BY rowid").fetchall() == original_views
        assert case.connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'context_views_%creation_sequence'",
        ).fetchall() == []
    else:
        storage.migrate_schema(
            case.owner.thread_id, session_id=case.owner.session_id, to_version=26,
            migration_name="view_creation_025_026", migration_sql=VIEW_CREATION_MIGRATION_SQL,
        )
        assert case.connection.execute(
            "SELECT view_id,creation_sequence FROM context_views ORDER BY creation_sequence",
        ).fetchall() == expected_order
        case.restart()
        assert canonical_json_bytes(case.snapshot({"assembly_id": assembly.assembly_id}).to_dict()) == original_assembly
    assert jsonl.read_bytes() == original_jsonl


@pytest.mark.parametrize("failure_kind", ["native", "sqlite", "json", "interrupted"])
def test_schema_migration_failure_journal_keeps_diagnostics_out_of_storage(
    epoch_case, monkeypatch, caplog: pytest.LogCaptureFixture, failure_kind: str,
):
    case = epoch_case
    case.load_snapshot()
    case.snapshot(case.prepare("migration-failure-journal"))
    case.checkpoint("migration-failure-checkpoint")
    storage = case.saver._storage.storage
    session, thread = case.owner.session_id, case.owner.thread_id
    root = storage.root(session, thread_id=thread)
    jsonl = root / "rollout.jsonl"
    original_jsonl = jsonl.read_bytes()
    restore_schema25(case.connection)
    # 迁移失败只能追加 journal 和改变恢复标记，不能改写既有业务或封存事实。
    preserved = {
        table: case.connection.execute(f"SELECT * FROM {table}").fetchall()
        for table in (
            "checkpoints", "checkpoint_channels", "checkpoint_state_values",
            "context_views", "context_assemblies", "assembly_content_nodes",
            "item_catalog", "turn_records", "executions", "storage_commits",
        )
    }
    original_journal = case.connection.execute(
        "SELECT * FROM schema_migrations ORDER BY migration_id",
    ).fetchall()
    native_path = str(root / "private-migration.sql")
    failure: BaseException
    if failure_kind == "native":
        failure = OSError(errno.EACCES, "迁移拒绝访问", native_path)
        fact = f"PermissionError, errno={errno.EACCES}"
    elif failure_kind == "sqlite":
        sqlite_error = sqlite3.OperationalError(f"SQLite 拒绝输入: {native_path}")
        sqlite_error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        failure = sqlite_error
        fact = f"OperationalError, sqlite_errorcode={sqlite3.SQLITE_IOERR}"
    elif failure_kind == "json":
        failure = json.JSONDecodeError(f"迁移输入非法: {native_path}", "{}\n?", 3)
        fact = "JSONDecodeError, lineno=2, colno=1, pos=3"
    else:
        failure = KeyboardInterrupt(f"迁移被中断: {native_path}")
        fact = "KeyboardInterrupt"
    migrate = rollout_maintenance_owner.execute_atomic_schema_sql

    def fail_after_ddl(connection, script):
        migrate(connection, script)
        assert connection.execute(
            "SELECT creation_sequence FROM context_views",
        ).fetchall()
        raise failure

    monkeypatch.setattr(rollout_maintenance_owner, "execute_atomic_schema_sql", fail_after_ddl)
    with (
        caplog.at_level(logging.ERROR, logger="app.core.failures"),
        pytest.raises(type(failure)) as caught,
    ):
        storage.migrate_schema(
            thread, session_id=session, to_version=26,
            migration_name="view_creation_025_026", migration_sql=VIEW_CREATION_MIGRATION_SQL,
        )
    assert caught.value is failure
    expected = f"SQLite 迁移失败: stage=rollout-schema-migration:25:26, error={fact}"
    # 从新SQLite连接核对已提交状态，不能依赖原连接中的事务或缓存。
    with closing(sqlite3.connect(sqlite_file_uri(root / "index.sqlite") + "?mode=ro", uri=True)) as cold:
        assert cold.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert cold.execute(
            "SELECT schema_version,database_state,session_id,thread_id FROM database_meta",
        ).fetchone() == (25, "recovery_required", session, thread)
        assert cold.execute(
            "SELECT from_version,to_version,migration_name,status,error_message "
            "FROM schema_migrations ORDER BY migration_id DESC LIMIT 1",
        ).fetchone() == (25, 26, "view_creation_025_026", "failed", expected)
        assert cold.execute(
            "SELECT * FROM schema_migrations ORDER BY migration_id",
        ).fetchall()[:-1] == original_journal
        for table, rows in preserved.items():
            assert cold.execute(f"SELECT * FROM {table}").fetchall() == rows
        assert cold.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'context_views_%creation_sequence'",
        ).fetchall() == []
    assert jsonl.read_bytes() == original_jsonl
    assert len([
        record for record in caplog.records
        if record.exc_info and record.exc_info[1] is failure
    ]) == 1
    assert str(failure) in caplog.text
