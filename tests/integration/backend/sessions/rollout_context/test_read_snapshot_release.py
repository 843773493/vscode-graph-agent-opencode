"""快照建立、关闭失败后仍释放原 SQLite 连接和 rollout 文件锁。"""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.infrastructure.rollout_context.storage.primitives import (
    _RolloutFileLock,
    _RolloutOperationLock,
    _RolloutSQLiteConnection,
)
from tests.integration.backend.sessions.rollout_context.test_schema4_crash_reader import (
    ReaderCase,
)
from tests.integration.backend.sessions.rollout_context.test_schema4_crash_reader import (
    reader_case as reader_case,  # noqa: PLC0414
)


def _assert_writer_available(case: ReaderCase) -> None:
    path = case.index.parent.parent / ".rollout.write.lock"

    def write_from_another_thread() -> None:
        with _RolloutOperationLock(path, timeout_seconds=0.2), sqlite3.connect(case.index, timeout=0.2) as writer:
            writer.execute("BEGIN IMMEDIATE")
            assert writer.execute("SELECT COUNT(*) FROM turns").fetchone() == (1,)
            writer.rollback()

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(write_from_another_thread).result(timeout=2)


def _contains_error(error: BaseException, expected: BaseException) -> bool:
    while error is not expected:
        previous = error.__cause__ or error.__context__
        if previous is None:
            return False
        error = previous
    return True


@pytest.mark.parametrize("fault", ["rollback", "close", "rollback_and_close"])
def test_read_snapshot_cleanup_attempts_every_release(
    reader_case: ReaderCase, fault: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = reader_case
    storage = case.saver._storage.storage
    canonical = storage.jsonl_path(case.session, thread_id=case.thread)
    original = canonical.read_bytes()
    items = storage.read_items(case.session, case.thread)
    snapshot = storage.open_read_snapshot(case.session, case.thread)
    assert snapshot.connection.in_transaction
    rollback_error = sqlite3.OperationalError("读取事务回滚失败")
    close_error = sqlite3.OperationalError("读取连接关闭失败")
    rollback, close = sqlite3.Connection.rollback, sqlite3.Connection.close
    calls: list[str] = []

    def failed_rollback(connection: _RolloutSQLiteConnection) -> None:
        rollback(connection)
        if connection is snapshot.connection:
            calls.append("rollback")
            if fault != "close":
                raise rollback_error

    def failed_close(connection: _RolloutSQLiteConnection) -> None:
        close(connection)
        if connection is snapshot.connection:
            calls.append("close")
            if fault != "rollback":
                raise close_error

    try:
        with monkeypatch.context() as patch:
            patch.setattr(_RolloutSQLiteConnection, "rollback", failed_rollback)
            patch.setattr(_RolloutSQLiteConnection, "close", failed_close)
            with pytest.raises(sqlite3.OperationalError) as caught:
                snapshot.close()
        assert caught.value is (rollback_error if fault == "rollback" else close_error)
        if fault == "rollback_and_close":
            assert _contains_error(caught.value, rollback_error)
        assert calls == ["rollback", "close"]
        assert snapshot.closed
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            snapshot.connection.execute("SELECT 1")
        _assert_writer_available(case)
        snapshot.close()
        assert calls == ["rollback", "close"]
        assert canonical.read_bytes() == original
        assert storage.read_items(case.session, case.thread) == items
    finally:
        close(snapshot.connection)
        snapshot.file_lock.release()


@pytest.mark.parametrize("fault", ["sqlite", "validation", "cancelled"])
def test_failed_snapshot_construction_releases_connection_and_lock(
    reader_case: ReaderCase, fault: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = reader_case
    storage = case.saver._storage.storage
    maintenance = storage._maintenance_owner
    canonical = storage.jsonl_path(case.session, thread_id=case.thread)
    original = canonical.read_bytes()
    items = storage.read_items(case.session, case.thread)
    original_error = {
        "sqlite": sqlite3.DatabaseError("快照 schema 读取失败"),
        "validation": ValueError("快照 schema 不一致"),
        "cancelled": asyncio.CancelledError("取消快照建立"),
    }[fault]
    close_error = sqlite3.OperationalError("失败清理时连接关闭失败")
    close = sqlite3.Connection.close
    connect, read_lock = storage._connect, maintenance.read_lock
    connections: list[sqlite3.Connection] = []
    locks: list[_RolloutFileLock] = []
    closed: list[sqlite3.Connection] = []

    def capture_connect(
        thread_id: str, checkpoint_ns: str = "", *, session_id: str, read_only: bool = False,
    ) -> sqlite3.Connection:
        connection = connect(thread_id, checkpoint_ns, session_id=session_id, read_only=read_only)
        connections.append(connection)
        return connection

    def capture_lock(session_id: str, thread_id: str, checkpoint_ns: str = "") -> _RolloutFileLock:
        lock = read_lock(session_id, thread_id, checkpoint_ns)
        locks.append(lock)
        return lock

    def reject_schema(_connection: sqlite3.Connection) -> None:
        raise original_error

    def failed_close(connection: _RolloutSQLiteConnection) -> None:
        close(connection)
        closed.append(connection)
        if fault != "cancelled":
            raise close_error

    try:
        with monkeypatch.context() as patch:
            patch.setattr(storage, "_connect", capture_connect)
            patch.setattr(maintenance, "read_lock", capture_lock)
            patch.setattr(maintenance, "_validate_schema_state", reject_schema)
            patch.setattr(_RolloutSQLiteConnection, "close", failed_close)
            with pytest.raises(BaseException) as caught:
                storage.open_read_snapshot(case.session, case.thread)
        assert caught.value is (original_error if fault == "cancelled" else close_error)
        assert _contains_error(caught.value, original_error)
        assert len(connections) == 1 and closed == connections
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connections[0].execute("SELECT 1")
        _assert_writer_available(case)
        assert canonical.read_bytes() == original
        assert storage.read_items(case.session, case.thread) == items
    finally:
        for connection in connections:
            close(connection)
        for lock in locks:
            lock.release()
