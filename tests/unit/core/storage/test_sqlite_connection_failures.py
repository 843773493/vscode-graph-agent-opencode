"""连接发布前的原生故障必须关闭句柄，并保全原库和完整原因链。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing
from functools import partial
from pathlib import Path

import pytest

from app.core import sqlite_state
from tests.unit.core.storage.test_sqlite_errors import (
    diagnostic_sink as diagnostic_sink,  # noqa: PLC0414
)
from tests.unit.core.storage.test_sqlite_errors import (
    state_migrations as state_migrations,  # noqa: PLC0414
)
from tests.unit.core.storage.test_sqlite_errors import (
    state_path as state_path,  # noqa: PLC0414
)

_PRIVATE = "/private/sqlite-connection-body"


@pytest.fixture
def connections(state_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[sqlite3.Connection]]:
    """保留真实连接供独立关闭证明；失败场景由测试显式回收仍存活的原句柄。"""
    opened: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    def tracked(database, *args, **kwargs) -> sqlite3.Connection:
        connection = connect(database, *args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite_state.sqlite3, "connect", tracked)
    try:
        yield opened
    finally:
        for connection in opened:
            sqlite3.Connection.close(connection)


@pytest.mark.parametrize("statement", [
    "PRAGMA foreign_keys = ON", "PRAGMA busy_timeout = 5000",
    "PRAGMA journal_mode = WAL", "PRAGMA synchronous = NORMAL",
])
def test_prepare_failure_closes_unpublished_connection_and_retries(
    state_path: Path, state_migrations: tuple[str, ...], connections: list[sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch, statement: str,
    diagnostic_sink: Callable[[BaseException], None],
) -> None:
    before = state_path.read_bytes()
    native: list[sqlite3.Error] = []
    original_connect = sqlite3.connect

    class RejectedPrepare(sqlite3.Connection):
        def execute(self, sql: str, parameters: Sequence[object] | Mapping[str, object] = ()) -> sqlite3.Cursor:
            if sql == statement:
                try:
                    super().execute(f'SELECT * FROM "{_PRIVATE}"')
                except sqlite3.Error as error:
                    native.append(error)
                    raise
            return super().execute(sql, parameters)

    with monkeypatch.context() as fault:
        fault.setattr(sqlite3, "connect", partial(original_connect, factory=RejectedPrepare))
        with pytest.raises(sqlite3.OperationalError) as caught:
            sqlite_state.SQLiteStateDatabase(path=state_path, schema_version=2, migrations=state_migrations)
    connection, = connections
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        sqlite3.Connection.execute(connection, "SELECT 1")
    assert caught.value.__cause__ is native[0]
    assert caught.value.sqlite_errorcode == native[0].sqlite_errorcode
    assert caught.value.sqlite_errorname == native[0].sqlite_errorname
    assert "stage=connection_prepare" in str(caught.value) and _PRIVATE not in str(caught.value)
    diagnostic_sink(caught.value)
    assert state_path.read_bytes() == before
    with sqlite_state.SQLiteStateDatabase(path=state_path, schema_version=2, migrations=state_migrations) as database:
        with closing(database.connection()) as recovered:
            assert recovered.execute("SELECT value FROM marker").fetchone()[0] == "原始 Ω"


@pytest.mark.parametrize("fault", ["directory", "open"])
def test_native_creation_failure_preserves_type_code_and_explicit_retry(
    tmp_path: Path, state_migrations: tuple[str, ...], fault: str,
    diagnostic_sink: Callable[[BaseException], None],
) -> None:
    blocker = tmp_path / "blocked"
    if fault == "directory":
        blocker.write_bytes(b"original")
        path = blocker / "state.sqlite"
        expected = FileExistsError
    else:
        blocker.mkdir()
        path = blocker
        expected = sqlite3.OperationalError
    with pytest.raises(expected) as caught:
        sqlite_state.SQLiteStateDatabase(path=path, schema_version=2, migrations=state_migrations)
    assert type(caught.value) is expected and type(caught.value.__cause__) is expected
    assert str(blocker) not in str(caught.value)
    assert f"stage={'database_directory' if fault == 'directory' else 'connection_open'}" in str(caught.value)
    diagnostic_sink(caught.value)
    if fault == "directory":
        assert blocker.read_bytes() == b"original"
        blocker.unlink()
    else:
        assert list(blocker.iterdir()) == []
        blocker.rmdir()
    with sqlite_state.SQLiteStateDatabase(path=path, schema_version=2, migrations=state_migrations) as database:
        assert database.diagnostics().applied_migrations == (1, 2)


@pytest.mark.parametrize("rollback_failure", [False, True])
@pytest.mark.parametrize("close_failure", [False, True])
def test_migration_rollback_and_close_failures_keep_native_order_and_original_rows(
    state_path: Path, state_migrations: tuple[str, ...], connections: list[sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch, rollback_failure: bool, close_failure: bool,
    diagnostic_sink: Callable[[BaseException], None],
) -> None:
    before = state_path.read_bytes()
    failures: list[sqlite3.Error] = []
    original_connect = sqlite3.connect

    class FailedCleanup(sqlite3.Connection):
        def rollback(self) -> None:
            super().rollback()
            if rollback_failure:
                error = sqlite3.OperationalError(f"rollback {_PRIVATE}")
                error.sqlite_errorcode = sqlite3.SQLITE_IOERR
                error.sqlite_errorname = "SQLITE_IOERR"
                failures.append(error)
                raise error

        def close(self) -> None:
            super().close()
            if close_failure:
                error = sqlite3.OperationalError(f"close {_PRIVATE}")
                error.sqlite_errorcode = sqlite3.SQLITE_IOERR_CLOSE
                error.sqlite_errorname = "SQLITE_IOERR_CLOSE"
                failures.append(error)
                raise error

    def migrate(connection: sqlite3.Connection) -> None:
        connection.execute("INSERT INTO marker VALUES ('不得提交')")
        try:
            connection.execute(f'SELECT * FROM "{_PRIVATE}"')
        except sqlite3.Error as error:
            failures.append(error)
            raise

    with monkeypatch.context() as fault:
        fault.setattr(sqlite3, "connect", partial(original_connect, factory=FailedCleanup))
        with pytest.raises(sqlite3.OperationalError) as caught:
            sqlite_state.SQLiteStateDatabase(path=state_path, schema_version=3, migrations=(*state_migrations, migrate))
    assert len(failures) == 1 + rollback_failure + close_failure
    diagnostic_sink(caught.value)
    assert caught.value.__cause__ is failures[-1]
    assert caught.value.sqlite_errorcode == failures[-1].sqlite_errorcode
    assert _PRIVATE not in str(caught.value) and str(state_path) not in str(caught.value)
    chain: list[BaseException] = []
    current: BaseException | None = caught.value
    while current is not None:
        chain.append(current)
        current = current.__cause__ or current.__context__
    assert [error for error in reversed(chain) if error in failures] == failures
    connection, = connections
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        sqlite3.Connection.execute(connection, "SELECT 1")
    assert state_path.read_bytes() == before
    with sqlite_state.SQLiteStateDatabase(path=state_path, schema_version=2, migrations=state_migrations) as recovered:
        assert recovered.diagnostics().applied_migrations == (1, 2)
        with closing(recovered.connection()) as connection:
            assert [row[0] for row in connection.execute("SELECT value FROM marker")] == ["原始 Ω"]


@pytest.mark.parametrize("fault", ["read", "close"])
def test_diagnostics_failure_closes_original_connection_without_disabling_database(
    state_path: Path, state_migrations: tuple[str, ...], connections: list[sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch, fault: str,
    diagnostic_sink: Callable[[BaseException], None],
) -> None:
    before = state_path.read_bytes()
    original_connect = sqlite3.connect

    class FailedDiagnostics(sqlite3.Connection):
        def execute(self, sql: str, parameters: Sequence[object] | Mapping[str, object] = ()) -> sqlite3.Cursor:
            if fault == "read" and sql.startswith("SELECT version"):
                return super().execute(f'SELECT * FROM "{_PRIVATE}"')
            return super().execute(sql, parameters)

        def close(self) -> None:
            super().close()
            if fault == "close":
                error = sqlite3.OperationalError(f"close {_PRIVATE}")
                error.sqlite_errorcode = sqlite3.SQLITE_IOERR_CLOSE
                error.sqlite_errorname = "SQLITE_IOERR_CLOSE"
                raise error

    with sqlite_state.SQLiteStateDatabase(path=state_path, schema_version=2, migrations=state_migrations) as database:
        with monkeypatch.context() as failure:
            failure.setattr(sqlite3, "connect", partial(original_connect, factory=FailedDiagnostics))
            with pytest.raises(sqlite3.OperationalError) as caught:
                database.diagnostics()
        diagnostic_sink(caught.value)
        assert f"stage={'diagnostics' if fault == 'read' else 'connection_close'}" in str(caught.value)
        assert _PRIVATE not in str(caught.value) and str(state_path) not in str(caught.value)
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            sqlite3.Connection.execute(connections[-1], "SELECT 1")
        assert database.diagnostics().applied_migrations == (1, 2)
    assert state_path.read_bytes() == before


def test_preparation_cancellation_closes_connection_and_preserves_original_interrupt(
    state_path: Path, state_migrations: tuple[str, ...], connections: list[sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = sqlite3.connect
    interruption = KeyboardInterrupt("取消连接准备")

    class InterruptedPrepare(sqlite3.Connection):
        def execute(self, sql: str, parameters: Sequence[object] | Mapping[str, object] = ()) -> sqlite3.Cursor:
            raise interruption

    with monkeypatch.context() as fault:
        fault.setattr(sqlite3, "connect", partial(original_connect, factory=InterruptedPrepare))
        with pytest.raises(KeyboardInterrupt) as caught:
            sqlite_state.SQLiteStateDatabase(path=state_path, schema_version=2, migrations=state_migrations)
    assert caught.value is interruption
    connection, = connections
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        sqlite3.Connection.execute(connection, "SELECT 1")
    with sqlite_state.SQLiteStateDatabase(path=state_path, schema_version=2, migrations=state_migrations):
        pass
