"""初始化复用本次连接，后续调用仍拒绝新出现的结构与发布故障。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.identifier import create_uuid_hex
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from tests.support.catalog_session_bundle import seed_catalog_session_bundle


@pytest.fixture
def rollout(tmp_path: Path) -> tuple[RolloutContextStore, str, str]:
    sessions = tmp_path / ".boxteam/sessions"
    sessions.mkdir(parents=True)
    session_id = f"ses_{create_uuid_hex()}"
    seed_catalog_session_bundle(sessions, session_id, title="初始化连接")
    store = RolloutContextStore(sessions)
    return store, session_id, store.storage.main_thread_id(session_id)


def test_initialization_opens_only_preflight_and_writer_connections(
    rollout: tuple[RolloutContextStore, str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, session_id, thread_id = rollout
    connections: list[sqlite3.Connection] = []
    connect = store.storage._connect

    def track(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(store.storage, "_connect", track)
    initial = store.storage.initialize(session_id, thread_id=thread_id)
    assert len(connections) == 1
    connections.clear()
    assert store.storage.initialize(session_id, thread_id=thread_id) == initial
    assert len(connections) == 2
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("damage", ["migrating", "reasoning"])
def test_repeated_initialization_still_rejects_damage_before_namespace_write(
    rollout: tuple[RolloutContextStore, str, str], damage: str,
) -> None:
    store, session_id, thread_id = rollout
    store.storage.initialize(session_id, thread_id=thread_id)
    index = store.storage.index_path(session_id, thread_id=thread_id)
    canonical = store.storage.jsonl_path(session_id, thread_id=thread_id)
    original = canonical.read_bytes()
    with sqlite3.connect(index) as connection:
        if damage == "migrating":
            connection.execute("UPDATE database_meta SET database_state='migrating'")
        else:
            connection.execute("ALTER TABLE reasoning_blocks RENAME COLUMN carrier_type TO removed_carrier")
    message = "rollout-publication-incomplete" if damage == "migrating" else "reasoning_blocks schema 不完整"
    with pytest.raises(RuntimeError, match=message):
        store.storage.initialize(session_id, "new-namespace", thread_id=thread_id)
    assert canonical.read_bytes() == original
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT 1 FROM checkpoint_namespace_state WHERE checkpoint_ns='new-namespace'").fetchone() is None
