"""测试 catalog fixture 对真实 SQLite 连接的归属和释放。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from tests.support.session_catalog_owner import fixture_catalog_scope


def test_relative_catalog_root_reuses_owner_and_releases_only_that_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    workspace_id = load_or_create_workspace_id(workspace)
    other_workspace_id = load_or_create_workspace_id(other_workspace)
    monkeypatch.chdir(tmp_path)
    relative_root = Path("workspace/.boxteam/sessions")
    absolute_root = workspace / ".boxteam" / "sessions"

    with fixture_catalog_scope() as owner:
        components = owner.get_catalog_components(relative_root, workspace_id)
        assert owner.get_catalog_components(absolute_root, workspace_id) is components
        connection = components.resolver.catalog_store.connection
        other = owner.get_catalog_components(
            other_workspace / ".boxteam" / "sessions", other_workspace_id,
        )
        other_connection = other.resolver.catalog_store.connection

        owner.release(relative_root)
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
        assert other_connection.execute("SELECT 1").fetchone()[0] == 1

        reopened = owner.get_catalog_components(absolute_root, workspace_id)
        reopened_connection = reopened.resolver.catalog_store.connection
        assert reopened is not components
        assert reopened_connection.execute("SELECT 1").fetchone()[0] == 1

    for closed_connection in (other_connection, reopened_connection):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            closed_connection.execute("SELECT 1")
