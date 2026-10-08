from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
)


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[AttachmentBlobCatalog]:
    value = AttachmentBlobCatalog(tmp_path / "catalog.sqlite")
    yield value
    value.close()


def test_initialization_failure_with_successful_close_allows_retry(
    catalog: AttachmentBlobCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = catalog._connect
    initialize = catalog._initialize
    connections: list[sqlite3.Connection] = []
    initialization_error = RuntimeError("first initialization failed")
    initialization_attempts = 0

    def capture_connection() -> sqlite3.Connection:
        connection = connect()
        connections.append(connection)
        return connection

    def fail_first_initialization(connection: sqlite3.Connection) -> None:
        nonlocal initialization_attempts
        initialization_attempts += 1
        if initialization_attempts == 1:
            raise initialization_error
        initialize(connection)

    monkeypatch.setattr(catalog, "_connect", capture_connection)
    monkeypatch.setattr(catalog, "_initialize", fail_first_initialization)

    with pytest.raises(RuntimeError, match="first initialization failed") as caught:
        _ = catalog.connection

    assert caught.value is initialization_error
    assert catalog._connection is None
    assert catalog._closed is False
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")

    connection = catalog.connection
    assert connection is connections[1]
    assert initialization_attempts == 2
    assert connection.in_transaction is False
    catalog.close()


def test_initialization_failure_with_close_failure_isolates_catalog(
    catalog: AttachmentBlobCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(catalog.database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    initialization_error = RuntimeError("initialization failed")

    class CloseFailureBoundary:
        def execute(self, statement: str, *parameters: object):
            return connection.execute(statement, *parameters)

        def close(self) -> None:
            raise OSError("injected close failure")

    boundary = CloseFailureBoundary()
    connect_calls = 0

    def connect_with_close_boundary() -> CloseFailureBoundary:
        nonlocal connect_calls
        connect_calls += 1
        return boundary

    def initialize_then_fail(connection_boundary: CloseFailureBoundary) -> None:
        connection_boundary.execute(
            "CREATE TABLE partial_initialization (value TEXT)"
        )
        raise initialization_error

    monkeypatch.setattr(catalog, "_connect", connect_with_close_boundary)
    monkeypatch.setattr(catalog, "_initialize", initialize_then_fail)

    try:
        with pytest.raises(RuntimeError, match="initialization failed") as caught:
            _ = catalog.connection

        assert caught.value is initialization_error
        assert any("injected close failure" in note for note in initialization_error.__notes__)
        assert catalog._connection is None
        assert catalog._closed is True
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type='table' AND name='partial_initialization'"
        ).fetchone()[0] == 1

        with pytest.raises(RuntimeError, match="已关闭"):
            _ = catalog.connection
        with pytest.raises(RuntimeError, match="已关闭"):
            catalog.mark_claim_published(blob_id="blb_" + "a" * 64)
        assert connect_calls == 1
    finally:
        connection.close()
