from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.services.infrastructure.file_tree_shortcut_store import (
    FileTreeShortcutStore,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from tests.support.catalog_session_bundle import seed_catalog_session_bundle


class FileTreeShortcutFixture(NamedTuple):
    store: FileTreeShortcutStore
    shortcut_path: Path
    first_session_id: str
    second_session_id: str
    workspace_root: Path
    session_path_resolver: SessionCatalogPathResolver


@pytest.fixture
def file_tree_shortcuts(
    empty_workspace_scope: tuple[str, Path],
) -> FileTreeShortcutFixture:
    workspace_id, workspace_root = empty_workspace_scope
    sessions_root = workspace_root / ".boxteam" / "sessions"

    session_path_resolver = get_session_path_resolver(sessions_root)
    session_path_resolver.initialize()
    session_ids = (
        "ses_00000000400070008000000000000011",
        "ses_00000000400070008000000000000012",
    )
    for session_id in session_ids:
        seed_catalog_session_bundle(sessions_root, session_id, title=session_id)
    store = FileTreeShortcutStore(
        workspace_root=workspace_root,
        path_resolver=session_path_resolver,
        file_paths=WorkspaceFilePaths(
            workspace=workspace_root,
            workspace_id=workspace_id,
            bindings=None,
        ),
    )
    shortcut_path = workspace_root / "torch_home"
    shortcut_path.mkdir()
    return FileTreeShortcutFixture(
        store,
        shortcut_path,
        session_ids[0],
        session_ids[1],
        workspace_root,
        session_path_resolver,
    )


def test_session_shortcut_is_stored_inside_session_node(
    file_tree_shortcuts: FileTreeShortcutFixture,
) -> None:
    store = file_tree_shortcuts.store
    shortcut_path = file_tree_shortcuts.shortcut_path
    first_session_id = file_tree_shortcuts.first_session_id

    settings = store.add_session_shortcut(
        first_session_id,
        path=str(shortcut_path),
        label="Torch Home",
    )

    assert [(item.label, item.source) for item in settings.effective_shortcuts] == [
        ("Torch Home", "session")
    ]
    session_settings_path = (
        file_tree_shortcuts.session_path_resolver.resolve_session_node(first_session_id)
        / "ui"
        / "file-tree-shortcuts.json"
    )
    assert session_settings_path.is_file()


def test_upgrade_snapshots_current_defaults_for_existing_sessions(
    file_tree_shortcuts: FileTreeShortcutFixture,
) -> None:
    settings_path = (
        file_tree_shortcuts.session_path_resolver.resolve_session_node(
            file_tree_shortcuts.first_session_id
        )
        / "ui"
        / "file-tree-shortcuts.json"
    )
    settings_path.unlink()
    workspace_settings_path = (
        file_tree_shortcuts.workspace_root
        / ".boxteam"
        / "ui"
        / "file-tree-shortcuts.json"
    )
    workspace_settings_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_settings_path.write_text(
        json.dumps(
            {
                "version": 1,
                "shortcuts": [
                    {
                        "path": file_tree_shortcuts.store._file_paths.reference(file_tree_shortcuts.shortcut_path),
                        "label": "Torch Home",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    migrated_store = FileTreeShortcutStore(
        workspace_root=file_tree_shortcuts.workspace_root,
        path_resolver=file_tree_shortcuts.session_path_resolver,
        file_paths=file_tree_shortcuts.store._file_paths,
    )

    settings = migrated_store.read_session_shortcuts(file_tree_shortcuts.first_session_id)
    assert [(item.label, item.source) for item in settings.effective_shortcuts] == [
        ("Torch Home", "workspace")
    ]
    assert json.loads(settings_path.read_text(encoding="utf-8"))["version"] == 2


def test_default_shortcut_only_applies_to_sessions_created_after_change(
    file_tree_shortcuts: FileTreeShortcutFixture,
) -> None:
    store = file_tree_shortcuts.store
    shortcut_path = file_tree_shortcuts.shortcut_path
    first_session_id = file_tree_shortcuts.first_session_id
    second_session_id = file_tree_shortcuts.second_session_id
    store.add_session_shortcut(
        first_session_id,
        path=str(shortcut_path),
        label="Torch Home",
    )

    promoted = store.set_workspace_default(
        first_session_id,
        path=str(shortcut_path),
    )
    existing = store.read_session_shortcuts(second_session_id)

    third_session_id = "ses_00000000400070008000000000000013"
    seed_catalog_session_bundle(
        file_tree_shortcuts.workspace_root / ".boxteam" / "sessions",
        third_session_id,
        title=third_session_id,
    )
    store.handle_session_change("create", third_session_id)
    inherited = store.read_session_shortcuts(third_session_id)

    assert promoted.effective_shortcuts[0].source == "session"
    assert existing.effective_shortcuts == []
    assert [(item.label, item.source) for item in inherited.effective_shortcuts] == [
        ("Torch Home", "workspace")
    ]
    assert [item.label for item in promoted.default_shortcuts] == ["Torch Home"]
    assert (
        file_tree_shortcuts.workspace_root
        / ".boxteam"
        / "ui"
        / "file-tree-shortcuts.json"
    ).is_file()


def test_removing_default_does_not_change_existing_session_snapshots(
    file_tree_shortcuts: FileTreeShortcutFixture,
) -> None:
    store = file_tree_shortcuts.store
    shortcut_path = file_tree_shortcuts.shortcut_path
    first_session_id = file_tree_shortcuts.first_session_id
    store.add_session_shortcut(
        first_session_id,
        path=str(shortcut_path),
        label="Torch Home",
    )
    store.set_workspace_default(first_session_id, path=str(shortcut_path))

    inherited_session_id = "ses_00000000400070008000000000000014"
    seed_catalog_session_bundle(
        file_tree_shortcuts.workspace_root / ".boxteam" / "sessions",
        inherited_session_id,
        title=inherited_session_id,
    )
    store.create_session_snapshot(inherited_session_id)

    store.remove_workspace_default(
        first_session_id,
        path=str(shortcut_path),
    )

    assert store.read_session_shortcuts(first_session_id).effective_shortcuts[0].source == "session"
    inherited = store.read_session_shortcuts(inherited_session_id)
    assert inherited.effective_shortcuts[0].source == "workspace"
    assert inherited.default_shortcuts == []

    store.remove_session_shortcut(
        inherited_session_id,
        path=str(shortcut_path),
    )
    assert store.read_session_shortcuts(inherited_session_id).effective_shortcuts == []


def test_shortcut_rejects_relative_or_missing_directories(
    file_tree_shortcuts: FileTreeShortcutFixture,
) -> None:
    store = file_tree_shortcuts.store
    shortcut_path = file_tree_shortcuts.shortcut_path
    first_session_id = file_tree_shortcuts.first_session_id

    relative = store.add_session_shortcut(first_session_id, path="torch_home", label=None)
    assert relative.effective_shortcuts[0].path == store._file_paths.reference(shortcut_path)

    with pytest.raises(FileNotFoundError, match="不存在"):
        store.add_session_shortcut(
            first_session_id,
            path=str(shortcut_path / "missing"),
            label=None,
        )
