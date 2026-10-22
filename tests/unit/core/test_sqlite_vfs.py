"""在实际 Python SQLite 上核验路径预算与原生库绑定。"""

from __future__ import annotations

import sqlite3

from app.core.sqlite_state import sqlite_vfs_max_pathname, validate_sqlite_path_budget


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
