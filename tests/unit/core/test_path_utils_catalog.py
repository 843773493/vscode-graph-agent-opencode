"""path_utils 的 SQLite catalog authority 契约测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.core.path_utils import (
    get_session_file,
    get_session_path,
    get_session_path_resolver,
    initialize_directories,
)
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from tests.support.catalog_session_bundle import seed_catalog_session_bundle


def make_standard_workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace_root = tmp_path / "workspace"
    return workspace_root, workspace_root / ".boxteam" / "sessions"


def seed_legacy_index(sessions_root: Path) -> Path:
    index_path = sessions_root.parent / "navigation" / "session-catalog-index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "revision": 1,
                "nodes": [
                    {
                        "node_id": "ses_legacy000000000000000000000000",
                        "kind": "session",
                        "name": "旧索引会话",
                        "parent_node_id": None,
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return index_path


def seed_corrupt_catalog(sessions_root: Path) -> Path:
    database_path = sessions_root.parent / "navigation" / "session-catalog.sqlite"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA user_version = 99")
    finally:
        connection.close()
    return database_path


def test_existing_sqlite_returns_catalog_resolver(tmp_path):
    _, sessions_root = make_standard_workspace(tmp_path)
    database_path = sessions_root.parent / "navigation" / "session-catalog.sqlite"
    from app.core.session_catalog_store import SessionCatalogStore

    store = SessionCatalogStore(database_path, sessions_root)
    store.close()

    resolver = get_session_path_resolver(sessions_root)

    assert isinstance(resolver, SessionCatalogPathResolver)
    assert resolver.catalog_store.database_path == database_path.resolve()


def test_sqlite_wins_over_stale_legacy_index(tmp_path):
    _, sessions_root = make_standard_workspace(tmp_path)
    database_path = sessions_root.parent / "navigation" / "session-catalog.sqlite"
    from app.core.session_catalog_store import SessionCatalogStore

    store = SessionCatalogStore(database_path, sessions_root)
    store.close()
    seed_legacy_index(sessions_root)

    assert isinstance(get_session_path_resolver(sessions_root), SessionCatalogPathResolver)


def test_legacy_index_without_sqlite_fails_closed_with_migration_hint(tmp_path):
    _, sessions_root = make_standard_workspace(tmp_path)
    legacy_index = seed_legacy_index(sessions_root)

    with pytest.raises(RuntimeError, match="SessionCatalogMigrator"):
        get_session_path_resolver(sessions_root)

    assert legacy_index.is_file()


def test_fresh_workspace_initializes_empty_catalog(tmp_path):
    _, sessions_root = make_standard_workspace(tmp_path)

    resolver = get_session_path_resolver(sessions_root)

    assert isinstance(resolver, SessionCatalogPathResolver)
    assert (sessions_root.parent / "navigation" / "session-catalog.sqlite").is_file()
    assert resolver.list_nodes() == []
    resolver.initialize()


def test_corrupt_catalog_user_version_fails_closed(tmp_path):
    _, sessions_root = make_standard_workspace(tmp_path)
    seed_corrupt_catalog(sessions_root)

    with pytest.raises(RuntimeError, match="schema 版本未知"):
        get_session_path_resolver(sessions_root)


def test_workspace_identity_is_created_and_bound(tmp_path):
    workspace_root, sessions_root = make_standard_workspace(tmp_path)
    from app.core.workspace_identity import load_or_create_workspace_id

    expected_id = load_or_create_workspace_id(workspace_root)
    resolver = get_session_path_resolver(sessions_root)

    assert isinstance(resolver, SessionCatalogPathResolver)
    assert resolver._workspace_id == expected_id
    identity = json.loads(
        (workspace_root / ".boxteam" / "workspace-identity.json").read_text(
            encoding="utf-8"
        )
    )
    assert identity["workspace_id"] == expected_id


def test_convenience_functions_route_through_catalog_factory(tmp_path, monkeypatch):
    workspace_root, _ = make_standard_workspace(tmp_path)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    initialize_directories()
    get_session_path_resolver()
    session_id = "ses_7c9c9b4ad4c54c0eb9dcd4dabb96e67d"
    session_dir = seed_catalog_session_bundle(
        workspace_root / ".boxteam" / "sessions",
        session_id,
        title="便捷函数会话",
    ).directory

    assert get_session_path(session_id) == session_dir
    assert get_session_file(session_id) == session_dir / "session.json"


def test_non_standard_sessions_root_uses_sqlite_catalog(tmp_path):
    sessions_root = tmp_path / "custom-sessions"

    resolver = get_session_path_resolver(sessions_root)

    assert isinstance(resolver, SessionCatalogPathResolver)
    database_path = tmp_path / ".custom-sessions-session-navigation" / "session-catalog.sqlite"
    assert database_path.is_file()
    assert resolver.list_nodes() == []


def test_same_root_reuses_cached_catalog_instance(tmp_path):
    _, sessions_root = make_standard_workspace(tmp_path)

    first = get_session_path_resolver(sessions_root)
    second = get_session_path_resolver(sessions_root)

    assert first is second
    assert isinstance(second, SessionCatalogPathResolver)
