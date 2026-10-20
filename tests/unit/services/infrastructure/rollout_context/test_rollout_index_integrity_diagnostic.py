from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import BinaryIO

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.identifier import create_uuid_hex
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.storage import rollout_index_reads
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from tests.support.catalog_session_bundle import seed_catalog_session_bundle


@pytest.fixture
def storage_owner(tmp_path: Path) -> tuple[RolloutContextStore, str, str]:
    sessions_root = tmp_path / ".boxteam" / "sessions"
    sessions_root.mkdir(parents=True)
    session_id = f"ses_{create_uuid_hex()}"
    seed_catalog_session_bundle(sessions_root, session_id, title="integrity diagnostic")
    storage = RolloutContextStore(
        sessions_root,
        serde=JsonPlusSerializer(),
        message_codec=LangChainMessageCodec(),
    )
    thread_id = storage.storage.main_thread_id(session_id)
    storage.storage.initialize(session_id, thread_id=thread_id)
    return storage, session_id, thread_id


def _damage_database_page_count(index_path: Path) -> None:
    """追加一个未分配页，供 SQLite integrity_check 实际检出。"""
    with sqlite3.connect(index_path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    page_size = int.from_bytes(index_path.read_bytes()[16:18], byteorder="big")
    if page_size == 1:
        page_size = 65536
    with index_path.open("r+b") as database:
        database.seek(28)
        page_count = int.from_bytes(database.read(4), byteorder="big")
        database.seek(0, 2)
        database.write(bytes(page_size))
        database.seek(28)
        database.write((page_count + 1).to_bytes(4, byteorder="big"))
        database.flush()


def test_integrity_failure_reports_session_thread_path_and_releases_handles(
    storage_owner: tuple[RolloutContextStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage, session_id, thread_id = storage_owner
    index_path = storage.storage.index_path(session_id, thread_id=thread_id)
    _damage_database_page_count(index_path)

    opened_connections: list[sqlite3.Connection] = []
    original_connect = storage.storage._connect

    def track_connection(
        owner_thread_id: str,
        checkpoint_ns: str = "",
        *,
        session_id: str | None = None,
        read_only: bool = False,
    ) -> sqlite3.Connection:
        connection = original_connect(
            owner_thread_id,
            checkpoint_ns,
            session_id=session_id,
            read_only=read_only,
        )
        if read_only:
            opened_connections.append(connection)
        return connection

    read_locks: list[rollout_index_reads._RolloutFileLock] = []
    opened_lock_handles: list[BinaryIO] = []
    original_file_lock = rollout_index_reads._RolloutFileLock
    original_open_handle = original_file_lock._open_handle

    def track_lock_handle(lock: rollout_index_reads._RolloutFileLock) -> BinaryIO:
        handle = original_open_handle(lock)
        opened_lock_handles.append(handle)
        return handle

    def track_file_lock(
        path: Path,
        *,
        exclusive: bool,
    ) -> rollout_index_reads._RolloutFileLock:
        file_lock = original_file_lock(path, exclusive=exclusive)
        read_locks.append(file_lock)
        return file_lock

    monkeypatch.setattr(storage.storage, "_connect", track_connection)
    monkeypatch.setattr(original_file_lock, "_open_handle", track_lock_handle)
    monkeypatch.setattr(rollout_index_reads, "_RolloutFileLock", track_file_lock)

    with pytest.raises(RuntimeError, match="integrity_check 失败") as raised:
        storage.storage.validate_index(session_id, thread_id)

    assert str(index_path) in str(raised.value)
    assert len(opened_connections) == 1
    assert len(read_locks) == 1
    assert len(opened_lock_handles) == 1
    assert opened_lock_handles[0].closed
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened_connections[0].execute("SELECT 1")


def test_existing_index_error_reports_pair_path(
    storage_owner: tuple[RolloutContextStore, str, str],
) -> None:
    storage, session_id, thread_id = storage_owner
    index_path = storage.storage.index_path(session_id, thread_id=thread_id)
    index_path.write_bytes(b"not a SQLite database")

    with (
        pytest.raises(RuntimeError, match="recovery_required") as raised,
        storage.storage._existing_index_connection(
            thread_id,
            "",
            session_id=session_id,
        ),
    ):
        raise AssertionError("damaged SQLite must fail before yielding")

    assert str(index_path) in str(raised.value)
