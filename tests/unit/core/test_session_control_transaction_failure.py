from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.session_control_store import SessionControlStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SessionControlStore]:
    value = SessionControlStore(tmp_path / "session-control.sqlite")
    yield value
    value.close()


def test_commit_failure_rolls_back_and_allows_next_write(store: SessionControlStore) -> None:
    connection = store.connection
    connection.executescript(
        """
        CREATE TABLE deferred_parent (id INTEGER PRIMARY KEY);
        CREATE TABLE deferred_child (
            parent_id INTEGER REFERENCES deferred_parent(id)
                DEFERRABLE INITIALLY DEFERRED
        );
        """
    )

    with pytest.raises(sqlite3.IntegrityError), store._write_transaction() as transaction:
        transaction.execute("INSERT INTO deferred_child(parent_id) VALUES (1)")

    assert connection.in_transaction is False
    with store._write_transaction() as transaction:
        transaction.execute("INSERT INTO deferred_parent(id) VALUES (1)")
        transaction.execute("INSERT INTO deferred_child(parent_id) VALUES (1)")
    assert connection.in_transaction is False
    assert connection.execute("SELECT count(*) FROM deferred_child").fetchone()[0] == 1


def test_body_failure_rolls_back_before_next_transaction(store: SessionControlStore) -> None:
    with pytest.raises(ValueError, match="body failure"), store._write_transaction() as transaction:
        transaction.execute("CREATE TABLE rolled_back (id INTEGER)")
        raise ValueError("body failure")

    assert store.connection.in_transaction is False
    assert store.connection.execute(
        "SELECT name FROM sqlite_master WHERE name='rolled_back'"
    ).fetchone() is None
    with store._write_transaction() as transaction:
        transaction.execute("CREATE TABLE committed (id INTEGER)")
    assert store.connection.execute(
        "SELECT name FROM sqlite_master WHERE name='committed'"
    ).fetchone()[0] == "committed"


def test_rollback_failure_does_not_replace_original_exception(
    store: SessionControlStore,
) -> None:
    connection = store._connection

    class RollbackFailure:
        @property
        def in_transaction(self) -> bool:
            return connection.in_transaction

        def execute(self, statement: str):
            if statement == "ROLLBACK":
                raise OSError("rollback unavailable")
            return connection.execute(statement)

    try:
        store._connection = RollbackFailure()  # type: ignore[assignment]
        with pytest.raises(ValueError, match="original failure") as caught, store._write_transaction():
            raise ValueError("original failure")
        assert any("rollback unavailable" in note for note in caught.value.__notes__)
    finally:
        store._connection = connection
        if connection.in_transaction:
            connection.execute("ROLLBACK")
