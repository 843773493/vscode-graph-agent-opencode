from __future__ import annotations

import pytest

from app.core.sqlite_state import SQLiteStateDatabase


def test_sqlite_state_initializes_wal_and_migrations(tmp_path):
    database = SQLiteStateDatabase(
        path=tmp_path / "state.sqlite",
        schema_version=1,
        migrations=(
            "CREATE TABLE sample (value TEXT NOT NULL);",
        ),
    )

    try:
        diagnostics = database.diagnostics()
        assert diagnostics.schema_version == 1
        assert diagnostics.applied_migrations == (1,)
        assert diagnostics.journal_mode.lower() == "wal"
        connection = database.connection()
        try:
            connection.execute("INSERT INTO sample(value) VALUES ('ok')")
            assert connection.execute("SELECT value FROM sample").fetchone()[0] == "ok"
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        finally:
            connection.close()
    finally:
        database.close()


def test_sqlite_state_rejects_second_process_owner(tmp_path):
    first = SQLiteStateDatabase(
        path=tmp_path / "state.sqlite",
        schema_version=1,
        migrations=("CREATE TABLE sample (value TEXT NOT NULL);",),
    )
    try:
        with pytest.raises(RuntimeError, match="已被另一个进程占用"):
            SQLiteStateDatabase(
                path=tmp_path / "state.sqlite",
                schema_version=1,
                migrations=("CREATE TABLE sample (value TEXT NOT NULL);",),
            )
    finally:
        first.close()
