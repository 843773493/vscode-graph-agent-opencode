"""未交付的 Session 控制库连接必须释放，错误只发布逻辑位置和原生事实。"""

from __future__ import annotations

import errno
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.core.session_control_store import SessionControlStore


@dataclass
class ConnectionFault:
    statement: str
    error: sqlite3.OperationalError
    connections: list[sqlite3.Connection] = field(default_factory=list)
    closed: list[sqlite3.Connection] = field(default_factory=list)


@pytest.fixture
def connection_fault():
    original = sqlite3.connect
    tracked: list[ConnectionFault] = []

    def install(
        statement: str, database: Path, *, read_only: bool, patch: pytest.MonkeyPatch,
        close_error: sqlite3.OperationalError | None = None,
    ) -> ConnectionFault:
        failure = sqlite3.OperationalError(f"原生数据库拒绝 {database}")
        failure.sqlite_errorcode = sqlite3.SQLITE_IOERR
        failure.sqlite_errorname = "SQLITE_IOERR"
        fault = ConnectionFault(statement, failure)
        tracked.append(fault)

        class Connection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                if sql.lstrip().startswith(statement):
                    # 在真实连接内占有写锁；失败释放不能依赖 GC 或后继连接超时。
                    if not read_only and not self.in_transaction:
                        super().execute("BEGIN IMMEDIATE")
                    raise failure
                return super().execute(sql, parameters)

            def close(self) -> None:
                if close_error is not None:
                    raise close_error
                super().close()
                fault.closed.append(self)

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs, factory=Connection)
            fault.connections.append(connection)
            return connection

        patch.setattr(sqlite3, "connect", connect)
        return fault

    yield install
    for fault in tracked:
        for connection in fault.connections:
            if connection not in fault.closed:
                sqlite3.Connection.close(connection)


@pytest.mark.parametrize("statement,read_only", [
    ("PRAGMA user_version", False),
    ("SELECT name FROM sqlite_schema", False),
    ("PRAGMA foreign_keys", False),
    ("PRAGMA busy_timeout", False),
    ("PRAGMA journal_mode", False),
    ("PRAGMA query_only", True),
    ("CREATE TABLE IF NOT EXISTS thread_catalog", False),
])
def test_initialization_failure_closes_real_connection_and_allows_retry(
    tmp_path: Path, connection_fault, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, statement: str, read_only: bool,
) -> None:
    database = tmp_path / "原始位置" / "session-control.sqlite"
    if read_only:
        with closing(SessionControlStore(database)):
            pass
    with monkeypatch.context() as faults:
        fault = connection_fault(statement, database, read_only=read_only, patch=faults)
        with pytest.raises(sqlite3.OperationalError) as caught:
            SessionControlStore(database, read_only=read_only)
        assert fault.closed == fault.connections and len(fault.closed) == 1
        assert caught.value.__cause__ is fault.error
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_IOERR
        assert caught.value.sqlite_errorname == "SQLITE_IOERR"
        detail = str(caught.value)
        assert str(database) not in detail
        assert "artifact=session_control" in detail
        assert "sqlite_errorcode=10" in detail and "sqlite_errorname=SQLITE_IOERR" in detail
        assert any(record.exc_info and record.exc_info[1] is fault.error for record in caplog.records)
    with closing(SessionControlStore(database, read_only=read_only)) as restored:
        assert restored.connection.execute("PRAGMA user_version").fetchone()[0] == SessionControlStore.SCHEMA_VERSION
        if not read_only:
            with restored._write_transaction():
                pass
    assert str(database) in caplog.text


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("corruption", ["version", "nonempty_v0"])
def test_rejected_database_preserves_bytes_and_keeps_location_in_diagnostics(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, read_only: bool, corruption: str,
) -> None:
    database = tmp_path / "session-control.sqlite"
    private_object = str(tmp_path / "private-object")
    quoted_object = private_object.replace('"', '""')
    with closing(sqlite3.connect(database)) as external:
        external.execute(f'CREATE TABLE "{quoted_object}" (value TEXT)')
        if corruption == "version":
            external.execute("PRAGMA user_version = 6")
        external.commit()
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match="schema 版本不受支持") as caught:
        SessionControlStore(database, read_only=read_only)
    detail = str(caught.value)
    assert str(tmp_path) not in detail and private_object not in detail
    assert "artifact=session_control" in detail and "stage=schema_version" in detail
    assert "user_version=" in detail and "supported=18" in detail
    assert str(database) in caplog.text
    assert database.read_bytes() == before
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()
    with closing(sqlite3.connect(database)) as external:
        assert external.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_directory_failure_keeps_native_type_cause_and_zero_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "private" / "session-control.sqlite"
    failure = PermissionError(errno.EACCES, f"原生目录拒绝 {database.parent}", str(database.parent))
    mkdir = Path.mkdir

    def fail(path, *args, **kwargs):
        if path == database.parent:
            raise failure
        return mkdir(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "mkdir", fail)
        with pytest.raises(PermissionError) as caught:
            SessionControlStore(database)
    assert caught.value.__cause__ is failure
    assert caught.value.errno == errno.EACCES and caught.value.filename is None
    assert "artifact=session_control" in str(caught.value) and "stage=directory" in str(caught.value)
    assert str(tmp_path) not in str(caught.value) and str(database) in caplog.text
    assert not database.exists()
    with closing(SessionControlStore(database)) as restored:
        assert restored.connection.execute("PRAGMA user_version").fetchone()[0] == SessionControlStore.SCHEMA_VERSION


def test_closed_owner_error_has_no_native_location(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    owner = SessionControlStore(tmp_path / "session-control.sqlite")
    owner.close()
    with pytest.raises(RuntimeError, match="已关闭") as caught:
        _ = owner.connection
    assert str(tmp_path) not in str(caught.value)
    assert "artifact=session_control" in str(caught.value)
    assert str(owner.database_path) in caplog.text


def test_wrong_path_type_is_not_echoed() -> None:
    private = "/private/session-control.sqlite"
    with pytest.raises(TypeError, match="database_path 必须是 Path") as caught:
        SessionControlStore(private)  # type: ignore[arg-type]
    assert private not in str(caught.value)


def test_construction_and_close_failure_preserve_both_causes(
    tmp_path: Path, connection_fault, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "private/session-control.sqlite"
    close_error = sqlite3.OperationalError(f"原生关闭失败 {database}")
    with monkeypatch.context() as faults:
        fault = connection_fault("PRAGMA foreign_keys", database, read_only=False,
                                 patch=faults, close_error=close_error)
        with pytest.raises(sqlite3.OperationalError) as caught:
            SessionControlStore(database)
    assert caught.value.__cause__ is fault.error
    assert not fault.closed and len(fault.connections) == 1
    notes = caught.value.__notes__
    assert len(notes) == 1 and "stage=close" in notes[0]
    assert str(database) not in str(caught.value) and str(database) not in notes[0]
    assert fault.connections[0].in_transaction, "关闭失败不得冒充连接已经释放"
    assert any(record.exc_info and record.exc_info[1] is close_error for record in caplog.records)
    assert any(record.exc_info and record.exc_info[1] is fault.error for record in caplog.records)


@pytest.mark.parametrize("transaction", ["read", "write"])
def test_nested_transaction_failure_is_safe_and_keeps_outer_transaction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, transaction: str,
) -> None:
    with closing(SessionControlStore(tmp_path / "session-control.sqlite")) as store:
        with store._write_transaction() as connection:
            connection.execute("INSERT INTO lifecycle_fence VALUES (1, 'active', 1)")
            with (
                pytest.raises(RuntimeError, match="不可重入") as caught,
                store.read_transaction() if transaction == "read" else store._write_transaction(),
            ):
                pytest.fail("嵌套事务不得进入")
            assert str(store.database_path) not in str(caught.value)
            assert "artifact=session_control" in str(caught.value)
            assert connection.in_transaction
        assert store.get_fence() == ("active", 1)
        assert str(store.database_path) in caplog.text
