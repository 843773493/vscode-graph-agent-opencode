from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.session_lifecycle_gate import SessionOperationLease
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
)


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[AttachmentBlobCatalog]:
    value = AttachmentBlobCatalog(tmp_path / "catalog.sqlite", workspace_id="a475d596-039d-48db-a14c-a0cbf7b4b58c")
    try:
        yield value
    finally:
        value.close()


def _write_ingest_record(catalog: AttachmentBlobCatalog, suffix: str) -> None:
    ingest_id = f"ing_{hashlib.sha256(suffix.encode()).hexdigest()[:32]}"
    now = datetime.now(UTC)
    catalog.create_ingest(
        operation_id=f"attkey_{suffix}", ingest_id=ingest_id,
        session_id="ses_" + "a" * 32, thread_id="thr_" + "b" * 32,
        message_id="msg_" + "c" * 32,
        pin=SessionOperationLease(
            lease_id=f"lease_{suffix}", operation_kind="attachment_ingest", operation_identity=suffix,
            preimage_hash="d" * 64, captured_lifecycle_generation=1, holder_generation=1,
            fencing_token=1, state="active", revision=1, recovery_ref=None,
            created_at=now.isoformat(), updated_at=now.isoformat(),
        ),
        digest=hashlib.sha256(suffix.encode()).hexdigest(), length=len(suffix),
        file_name="probe.txt", mime_type="text/plain", now=now,
    )


def test_commit_failure_rolls_back_and_allows_next_public_write(
    catalog: AttachmentBlobCatalog,
) -> None:
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
        "SELECT count(*) FROM attachment_directory_ingests"
    ).fetchone()[0] == 1
    catalog.close()


def test_body_failure_rolls_back_and_allows_next_public_write(catalog: AttachmentBlobCatalog) -> None:
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
    catalog: AttachmentBlobCatalog,
    close_fails: bool,
) -> None:
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

    catalog._connections[threading.get_ident()] = RollbackFailureBoundary()  # type: ignore[assignment]
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
            close_fails = False
            catalog.close()
