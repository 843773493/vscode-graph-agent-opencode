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
    statements: list[str] = []
    connect = store.storage._connect

    def track(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store.storage, "_connect", track)
    initial = store.storage.initialize(session_id, thread_id=thread_id)
    assert len(connections) == 1
    schema_query = "SELECT schema_version, message_format_version FROM database_meta WHERE singleton_id = 1"
    assert statements.count(schema_query) == 1
    connections.clear()
    statements.clear()
    assert store.storage.initialize(session_id, thread_id=thread_id) == initial
    assert len(connections) == 2
    assert statements.count(schema_query) == 1
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("operation", ["initialize", "read_snapshot"])
def test_rollout_operation_resolves_thread_once_and_next_call_checks_live_owner(
    rollout: tuple[RolloutContextStore, str, str], operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, session_id, thread_id = rollout
    store.storage.initialize(session_id, thread_id=thread_id)
    resolver = store.storage._path_resolver
    resolve = resolver.resolve_thread_node
    resolved: list[tuple[str, str]] = []

    def track(session: str, thread: str) -> Path:
        resolved.append((session, thread))
        return resolve(session, thread)

    def run() -> None:
        if operation == "initialize":
            store.storage.initialize(session_id, thread_id=thread_id)
        else:
            with store.storage.open_read_snapshot(session_id, thread_id) as snapshot:
                assert (snapshot.session_id, snapshot.thread_id) == (session_id, thread_id)

    monkeypatch.setattr(resolver, "resolve_thread_node", track)
    run()
    assert resolved == [(session_id, thread_id)]
    resolved.clear()
    run()
    assert resolved == [(session_id, thread_id)]
    control = resolver.resolve_session_node(session_id) / "session-control.sqlite"
    with sqlite3.connect(control) as connection:
        connection.execute(
            "UPDATE thread_catalog SET thread_id=? WHERE kind='main'",
            (f"thr_{create_uuid_hex()}",),
        )
    with pytest.raises(RuntimeError, match="main_thread"):
        run()


def test_scoped_live_node_does_not_override_private_fork_source(
    rollout: tuple[RolloutContextStore, str, str], tmp_path: Path,
) -> None:
    store, session_id, thread_id = rollout
    paths = store.storage.fork_paths
    live = paths.thread_node(session_id, thread_id)
    captured = tmp_path / "captured-source"
    captured.mkdir()
    with paths.thread_resolution_scope():
        assert paths.thread_node(session_id, thread_id) == live
        with paths.fork_source_snapshot_scope(
            session_id=session_id, thread_id=thread_id, captured_root=captured,
        ):
            assert paths.thread_node(session_id, thread_id) == captured
            with pytest.raises(RuntimeError, match="只授予冻结 source Thread"):
                paths.thread_node(session_id, f"thr_{create_uuid_hex()}")
        assert paths.thread_node(session_id, thread_id) == live


@pytest.mark.parametrize("damage,message", [
    ("migrating", "rollout-publication-incomplete"),
    ("reasoning", "reasoning_blocks schema 不完整"),
    ("schema_journal", "checksum"),
    ("view_creation_index", "缺少 view 创建顺序结构"),
])
def test_repeated_initialization_still_rejects_damage_before_namespace_write(
    rollout: tuple[RolloutContextStore, str, str], damage: str, message: str,
) -> None:
    store, session_id, thread_id = rollout
    store.storage.initialize(session_id, thread_id=thread_id)
    index = store.storage.index_path(session_id, thread_id=thread_id)
    canonical = store.storage.jsonl_path(session_id, thread_id=thread_id)
    original = canonical.read_bytes()
    with sqlite3.connect(index) as connection:
        if damage == "migrating":
            connection.execute("UPDATE database_meta SET database_state='migrating'")
        elif damage == "reasoning":
            connection.execute("ALTER TABLE reasoning_blocks RENAME COLUMN carrier_type TO removed_carrier")
        elif damage == "schema_journal":
            connection.execute("UPDATE schema_migrations SET migration_checksum='broken' WHERE from_version=0")
        else:
            connection.execute("DROP INDEX context_views_creation_sequence")
    with pytest.raises(RuntimeError, match=message):
        store.storage.initialize(session_id, "new-namespace", thread_id=thread_id)
    assert canonical.read_bytes() == original
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT 1 FROM checkpoint_namespace_state WHERE checkpoint_ns='new-namespace'").fetchone() is None
