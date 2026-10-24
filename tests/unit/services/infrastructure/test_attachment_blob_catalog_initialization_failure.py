"""目录 catalog 初始化失败时保留原错、隔离未关闭连接并允许安全重试。"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.infrastructure.attachment_blob_catalog import (
    catalog as catalog_module,
)
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
)
from app.services.infrastructure.attachment_blob_catalog.maintenance import (
    AttachmentMaintenanceBusyError,
    AttachmentMaintenanceLock,
)


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[AttachmentBlobCatalog]:
    value = AttachmentBlobCatalog(tmp_path / "catalog.sqlite", workspace_id="a475d596-039d-48db-a14c-a0cbf7b4b58c")
    try:
        yield value
    finally:
        value.close()


def test_initialization_failure_with_successful_close_allows_retry(
    catalog: AttachmentBlobCatalog, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = sqlite3.connect
    initialize = catalog_module.initialize_directory_schema
    connections: list[sqlite3.Connection] = []
    initialization_error = RuntimeError("首次初始化失败")
    initialization_attempts = 0

    def capture_connection(*args, **kwargs) -> sqlite3.Connection:
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    def fail_first_initialization(connection: sqlite3.Connection, *, workspace_id: str) -> None:
        nonlocal initialization_attempts
        initialization_attempts += 1
        if initialization_attempts == 1:
            raise initialization_error
        initialize(connection, workspace_id=workspace_id)

    monkeypatch.setattr(sqlite3, "connect", capture_connection)
    monkeypatch.setattr(catalog_module, "initialize_directory_schema", fail_first_initialization)
    with pytest.raises(RuntimeError, match="首次初始化失败") as caught:
        _ = catalog.connection
    assert caught.value is initialization_error
    assert catalog._connections == {} and catalog._closed is False
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    with AttachmentMaintenanceLock(catalog.database_path.parent, exclusive=True):
        pass
    connection = catalog.connection
    assert connection is connections[1] and initialization_attempts == 2
    assert connection.in_transaction is False
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_initialization_failure_with_close_failure_isolates_catalog(
    catalog: AttachmentBlobCatalog, monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialization_error = RuntimeError("初始化失败")
    connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    class CloseFailureConnection(sqlite3.Connection):
        fail_close = True

        def close(self) -> None:
            if self.fail_close:
                raise OSError("连接关闭失败")
            super().close()

    def capture_connection(*args, **kwargs) -> sqlite3.Connection:
        connection = connect(*args, **kwargs, factory=CloseFailureConnection)
        connections.append(connection)
        return connection

    def initialize_then_fail(connection: sqlite3.Connection, *, workspace_id: str) -> None:
        connection.execute("CREATE TABLE partial_initialization (value TEXT)")
        raise initialization_error

    monkeypatch.setattr(sqlite3, "connect", capture_connection)
    monkeypatch.setattr(catalog_module, "initialize_directory_schema", initialize_then_fail)
    try:
        with pytest.raises(RuntimeError, match="初始化失败") as caught:
            _ = catalog.connection
        (connection,) = connections
        assert caught.value is initialization_error
        assert any("连接关闭失败" in note for note in initialization_error.__notes__)
        assert catalog._connections == {threading.get_ident(): connection} and catalog._closed is True
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name='partial_initialization'").fetchone()[0] == 1
        with pytest.raises(AttachmentMaintenanceBusyError), AttachmentMaintenanceLock(catalog.database_path.parent, exclusive=True):
            raise AssertionError("未关闭连接不能归还维护锁")
        with pytest.raises(RuntimeError, match="已关闭"):
            _ = catalog.connection
        with pytest.raises(RuntimeError, match="已关闭"):
            catalog.release_associations(session_id="ses_" + "a" * 32, now=datetime.now(UTC))
        assert len(connections) == 1
    finally:
        CloseFailureConnection.fail_close = False
        catalog.close()
    assert catalog._connections == {}
    with AttachmentMaintenanceLock(catalog.database_path.parent, exclusive=True):
        pass
