"""在实际 Python SQLite 上核验路径预算与原生库绑定。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing

import pytest

from app.core import sqlite_state
from app.core.atomic_fs import native_io_path
from app.core.sqlite_state import (
    SQLiteStateDatabase,
    sqlite_file_uri,
    sqlite_vfs_max_pathname,
    validate_sqlite_path_budget,
)


def test_python_sqlite_vfs_and_wal(tmp_path):
    database = tmp_path / "native.sqlite"
    maximum = sqlite_vfs_max_pathname()
    assert maximum > len(str(database.resolve()).encode("utf-8")) + len("-journal")
    validate_sqlite_path_budget(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT sqlite_version()").fetchone()[0] == sqlite3.sqlite_version
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE probe (value TEXT)")
        connection.execute("INSERT INTO probe VALUES (?)", ("真实 SQLite Ω",))
        connection.commit()
        assert connection.execute("SELECT value FROM probe").fetchone()[0] == "真实 SQLite Ω"
        assert database.with_name(database.name + "-wal").is_file()


def test_sqlite_file_uri_preserves_special_names_and_read_only(tmp_path):
    database = tmp_path / "报价 #%.sqlite"
    uri = sqlite_file_uri(database)
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES (?)", ("历史正文 Ω",))
        connection.commit()
    with closing(sqlite3.connect(uri + "?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "历史正文 Ω"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM sample")
    missing = tmp_path / "不存在.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(sqlite_file_uri(missing) + "?mode=ro", uri=True)
    assert not missing.exists()
    assert {path.name for path in tmp_path.iterdir()} == {database.name}


def test_sqlite_budget_counts_actual_native_path_and_sidecar(tmp_path, monkeypatch):
    database = tmp_path / "catalog.sqlite"
    maximum = len(str(native_io_path(database.resolve())).encode("utf-8")) + len("-journal")
    monkeypatch.setattr(sqlite_state, "sqlite_vfs_max_pathname", lambda: maximum)
    validate_sqlite_path_budget(database)
    monkeypatch.setattr(sqlite_state, "sqlite_vfs_max_pathname", lambda: maximum - 1)
    with pytest.raises(ValueError, match="-journal"):
        validate_sqlite_path_budget(database)
    assert not database.exists()


@pytest.fixture(params=[(258, "x"), (600, "x"), (300, "测")], ids=["sidecar-over-max-path", "long-native", "unicode-native"])
def windows_database_path(tmp_path, request):
    if os.name != "nt":
        pytest.skip("真实 Windows 原生路径与 WAL 边界")
    length, character = request.param
    database = tmp_path / "state.sqlite"
    padding = length - len(str(database))
    assert padding > 0, str(database)
    while padding:
        segment = min(100, padding - 1)
        if segment < 1:
            database = database.with_name("x" + database.name)
            padding -= 1
        else:
            database = database.parent / (character * segment) / database.name
            padding -= segment + 1
    assert len(str(database)) == length
    return database


def test_windows_sqlite_state_wal_owner_reopen_and_frozen_backup(windows_database_path):
    database = windows_database_path
    migrations = ("CREATE TABLE sample (value TEXT NOT NULL);",)
    with SQLiteStateDatabase(path=database, schema_version=1, migrations=migrations) as owner:
        with closing(owner.connection()) as writer:
            writer.execute("INSERT INTO sample VALUES (?)", ("真实 Windows WAL Ω",))
            assert native_io_path(database.with_name(database.name + "-wal")).is_file()
            with closing(sqlite3.connect(sqlite_file_uri(database) + "?mode=ro", uri=True)) as reader:
                assert reader.execute("SELECT value FROM sample").fetchone()[0] == "真实 Windows WAL Ω"
            backup = database.with_name("frozen.sqlite")
            with closing(sqlite3.connect(sqlite_file_uri(backup), uri=True)) as snapshot:
                writer.backup(snapshot)
        with pytest.raises(RuntimeError, match="已被另一个进程占用"):
            SQLiteStateDatabase(path=database, schema_version=1, migrations=migrations)
    with SQLiteStateDatabase(path=database, schema_version=1, migrations=migrations) as reopened:
        assert reopened.path == database.resolve()
        with closing(reopened.connection()) as reader:
            assert reader.execute("SELECT value FROM sample").fetchone()[0] == "真实 Windows WAL Ω"
    with closing(sqlite3.connect(sqlite_file_uri(backup) + "?mode=ro&immutable=1", uri=True)) as frozen:
        assert frozen.execute("SELECT value FROM sample").fetchone()[0] == "真实 Windows WAL Ω"
    assert not native_io_path(backup.with_name(backup.name + "-wal")).exists()
