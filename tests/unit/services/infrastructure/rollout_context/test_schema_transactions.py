"""通用 SQLite DDL 的外层事务、触发器与逃逸拒绝合同。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing

import pytest

from app.services.infrastructure.rollout_context.storage.schema_upgrade import (
    execute_atomic_schema_sql,
)


@pytest.fixture
def migration_connection() -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("BEGIN IMMEDIATE")
        yield connection


def test_atomic_script_handles_literal_and_trigger_semicolons(migration_connection: sqlite3.Connection) -> None:
    execute_atomic_schema_sql(migration_connection, """
        CREATE TABLE probe(value TEXT);
        CREATE TABLE audit(value TEXT);
        CREATE TRIGGER save_probe AFTER INSERT ON probe BEGIN
            INSERT INTO audit VALUES (new.value); INSERT INTO audit VALUES ('trigger;value');
        END;
        INSERT INTO probe VALUES ('literal;value');
    """)
    assert migration_connection.in_transaction
    assert migration_connection.execute("SELECT value FROM audit").fetchall() == [("literal;value",), ("trigger;value",)]
    migration_connection.rollback()
    assert not migration_connection.execute("SELECT name FROM sqlite_master WHERE name = 'probe'").fetchall()


@pytest.mark.parametrize("escape", ["COMMIT", "ROLLBACK", "SAVEPOINT migration_escape", "ATTACH ':memory:' AS other", "PRAGMA user_version=3"])
def test_migration_script_cannot_escape_transaction(migration_connection: sqlite3.Connection, escape: str) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        execute_atomic_schema_sql(migration_connection, f"CREATE TABLE probe(value); {escape};")
    assert migration_connection.in_transaction
    migration_connection.rollback()
    assert not migration_connection.execute("SELECT name FROM sqlite_master WHERE name = 'probe'").fetchall()


def test_checked_column_migration_uses_outer_transaction(migration_connection: sqlite3.Connection) -> None:
    execute_atomic_schema_sql(migration_connection, """
        CREATE TABLE probe(value TEXT);
        INSERT INTO probe VALUES ('existing');
        ALTER TABLE probe ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0 CHECK(sequence >= 0);
    """)
    assert migration_connection.in_transaction
    assert migration_connection.execute("SELECT value, sequence FROM probe").fetchall() == [("existing", 0)]
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        migration_connection.execute("UPDATE probe SET sequence = -1")
    migration_connection.rollback()
    assert not migration_connection.execute("SELECT name FROM sqlite_master WHERE name = 'probe'").fetchall()


def test_checked_column_does_not_authorize_script_pragma(migration_connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        execute_atomic_schema_sql(migration_connection, """
            CREATE TABLE probe(value TEXT);
            ALTER TABLE probe ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0 CHECK(sequence >= 0);
            PRAGMA quick_check(probe);
        """)
    assert migration_connection.in_transaction
    migration_connection.rollback()
    assert not migration_connection.execute("SELECT name FROM sqlite_master WHERE name = 'probe'").fetchall()
