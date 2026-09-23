from __future__ import annotations

import sqlite3

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


def test_sqlite_state_allows_explicit_shared_processes(tmp_path):
    first = SQLiteStateDatabase(
        path=tmp_path / "state.sqlite",
        schema_version=1,
        migrations=("CREATE TABLE sample (value TEXT NOT NULL);",),
        allow_shared_processes=True,
    )
    second = SQLiteStateDatabase(
        path=tmp_path / "state.sqlite",
        schema_version=1,
        migrations=("CREATE TABLE sample (value TEXT NOT NULL);",),
        allow_shared_processes=True,
    )
    try:
        first_connection = first.connection()
        second_connection = second.connection()
        try:
            first_connection.execute("INSERT INTO sample(value) VALUES ('first')")
            second_connection.execute("INSERT INTO sample(value) VALUES ('second')")
            values = {
                row[0]
                for row in second_connection.execute(
                    "SELECT value FROM sample ORDER BY value"
                ).fetchall()
            }
            assert values == {"first", "second"}
        finally:
            first_connection.close()
            second_connection.close()
    finally:
        second.close()
        first.close()


def test_sqlite_state_rejects_migration_ledger_gap(tmp_path):
    """迁移账本被外部改出缺号时必须响亮失败，不得静默忽略缺号迁移。"""

    path = tmp_path / "state.sqlite"
    database = SQLiteStateDatabase(
        path=path,
        schema_version=3,
        migrations=(
            "CREATE TABLE a (value TEXT NOT NULL);",
            "CREATE TABLE b (value TEXT NOT NULL);",
            "CREATE TABLE c (value TEXT NOT NULL);",
        ),
    )
    database.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM schema_migrations WHERE version = 2")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="迁移账本被外部改写"):
        SQLiteStateDatabase(
            path=path,
            schema_version=3,
            migrations=(
                "CREATE TABLE a (value TEXT NOT NULL);",
                "CREATE TABLE b (value TEXT NOT NULL);",
                "CREATE TABLE c (value TEXT NOT NULL);",
            ),
        )


def test_sqlite_state_rejects_migration_ledger_ahead_of_program(tmp_path):
    """账本版本高于程序支持的迁移数量时必须 fail-closed。"""

    path = tmp_path / "state.sqlite"
    database = SQLiteStateDatabase(
        path=path,
        schema_version=1,
        migrations=("CREATE TABLE a (value TEXT NOT NULL);",),
    )
    database.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (2, 'x')"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="高于当前程序支持范围"):
        SQLiteStateDatabase(
            path=path,
            schema_version=1,
            migrations=("CREATE TABLE a (value TEXT NOT NULL);",),
        )


def test_sqlite_state_rejects_lost_migrated_table_on_reopen(tmp_path):
    """已登记迁移建出的表被外部删掉时重开必须响亮失败，不得静默重建空表。

    若外部进程把某次迁移建出的表删除而 ``schema_migrations`` 仍登记该版本，
    此前 ``CREATE TABLE IF NOT EXISTS`` 会因``version <= current``被跳过，
    库以“健康”姿态打开，直到首次真实查询才报 ``no such table``。这既不是
    响亮失败也不是真实默认值。重开必须在建表前 fail closed 并指明缺表。
    """
    path = tmp_path / "state.sqlite"
    migrations = (
        "CREATE TABLE a (value TEXT NOT NULL);",
        "CREATE TABLE b (value TEXT NOT NULL);",
    )
    database = SQLiteStateDatabase(
        path=path, schema_version=2, migrations=migrations
    )
    database.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE b")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="b"):
        SQLiteStateDatabase(
            path=path, schema_version=2, migrations=migrations
        )
