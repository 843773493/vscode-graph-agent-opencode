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


@pytest.mark.parametrize("close_fails", [False, True])
def test_rollback_failure_closes_store_without_replacing_original_exception(
    store: SessionControlStore,
    close_fails: bool,
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

        def close(self) -> None:
            if close_fails:
                raise OSError("close unavailable")
            connection.close()

    failure = ValueError("original failure")
    try:
        store._connection = RollbackFailure()  # type: ignore[assignment]
        with pytest.raises(ValueError, match="original failure") as caught, store._write_transaction() as transaction:
            transaction.execute("CREATE TABLE rollback_failure_probe (id INTEGER)")
            raise failure
        assert caught.value is failure
        assert any("rollback unavailable" in note for note in caught.value.__notes__)
        if close_fails:
            assert any("close unavailable" in note for note in caught.value.__notes__)
            assert connection.in_transaction
        else:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")

        with pytest.raises(RuntimeError, match="已关闭"):
            _ = store.connection
        with pytest.raises(RuntimeError, match="已关闭"):
            store.get_collaboration_ledger_revision()
        with pytest.raises(RuntimeError, match="已关闭"):
            store.register_collaboration_member(
                delegation_id="del-after-rollback-failure",
                coordinator_session_id="ses-after-rollback-failure",
                coordinator_thread_id="thr-after-rollback-failure",
                role="delegated_subagent",
                subagent_type="general-purpose",
                title="回滚失败后写入",
                task_seed="{}",
            )
    finally:
        store._connection = connection
        if close_fails:
            connection.close()
