from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
)


def _catalog(tmp_path: Path) -> AttachmentBlobCatalog:
    return AttachmentBlobCatalog(tmp_path / "catalog.sqlite")


def _write_ingest_record(catalog: AttachmentBlobCatalog, suffix: str) -> None:
    ingest_id = f"ing_{hashlib.sha256(suffix.encode()).hexdigest()[:32]}"
    catalog.create_or_get_ingest_record(
        ingest_idempotency_key=f"attkey_{suffix}",
        ingest_id=ingest_id,
        owner_session_id="ses_" + "a" * 32,
        owner_thread_id="thr_" + "b" * 32,
        pin_lease_id=f"lease_{suffix}",
        pin_fencing_token=1,
        pin_captured_generation=1,
        preimage_hash="c" * 64,
        staging_relative_locator=f".staging/{ingest_id}",
        max_bytes=1024,
    )


def test_commit_failure_rolls_back_and_allows_next_public_write(
    tmp_path: Path,
) -> None:
    catalog = _catalog(tmp_path)
    connection = catalog.connection
    connection.executescript(
        """
        CREATE TABLE deferred_parent (id INTEGER PRIMARY KEY);
        CREATE TABLE deferred_child (
            parent_id INTEGER REFERENCES deferred_parent(id)
                DEFERRABLE INITIALLY DEFERRED
        );
        """
    )

    with pytest.raises(
        sqlite3.IntegrityError, match="FOREIGN KEY"
    ), catalog.write_transaction() as transaction:
        transaction.execute("INSERT INTO deferred_child(parent_id) VALUES (17)")

    assert connection.in_transaction is False
    assert connection.execute("SELECT count(*) FROM deferred_child").fetchone()[0] == 0
    assert catalog.connection is connection
    _write_ingest_record(catalog, "commit_recovery")
    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT count(*) FROM attachment_ingest_records"
    ).fetchone()[0] == 1
    catalog.close()


def test_body_failure_rolls_back_and_allows_next_public_write(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    connection = catalog.connection
    connection.execute("CREATE TABLE body_failure_probe (value TEXT)")
    failure = ValueError("body failure")

    with pytest.raises(ValueError) as caught, catalog.write_transaction() as transaction:
        transaction.execute(
            "INSERT INTO body_failure_probe(value) VALUES ('uncommitted')"
        )
        raise failure

    assert caught.value is failure
    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT count(*) FROM body_failure_probe"
    ).fetchone()[0] == 0
    _write_ingest_record(catalog, "body_recovery")
    assert connection.in_transaction is False
    catalog.close()


@pytest.mark.parametrize("close_fails", [False, True])
def test_rollback_failure_preserves_original_and_isolates_catalog(
    tmp_path: Path,
    close_fails: bool,
) -> None:
    catalog = _catalog(tmp_path)
    connection = catalog.connection
    connection.execute("CREATE TABLE rollback_failure_probe (value TEXT)")

    class RollbackFailureBoundary:
        @property
        def in_transaction(self) -> bool:
            return connection.in_transaction

        def execute(self, statement: str, *parameters: object):
            if statement == "ROLLBACK":
                raise OSError("rollback unavailable")
            return connection.execute(statement, *parameters)

        def close(self) -> None:
            if close_fails:
                raise OSError("close unavailable")
            connection.close()

    catalog._connection = RollbackFailureBoundary()  # type: ignore[assignment]
    failure = ValueError("original body failure")
    try:
        with pytest.raises(ValueError) as caught, catalog.write_transaction() as transaction:
            transaction.execute(
                "INSERT INTO rollback_failure_probe(value) VALUES ('pending')"
            )
            raise failure

        assert caught.value is failure
        assert any("rollback unavailable" in note for note in failure.__notes__)
        if close_fails:
            assert any("close unavailable" in note for note in failure.__notes__)
            assert connection.in_transaction is True
        else:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")

        with pytest.raises(RuntimeError, match="已关闭"):
            _ = catalog.connection
        with pytest.raises(RuntimeError, match="已关闭"):
            _write_ingest_record(catalog, "rollback_failure")
    finally:
        if close_fails:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
