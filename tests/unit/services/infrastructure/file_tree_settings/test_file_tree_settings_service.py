from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_catalog_resolver import SessionCatalogPathResolver
from app.services.infrastructure.file_tree_settings_service import (
    FileTreeSettingsService,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from tests.support.catalog_session_bundle import seed_catalog_session_bundle


class FileTreeSettingsFixture(NamedTuple):
    service: FileTreeSettingsService
    shortcut_path: Path
    first_session_id: str
    second_session_id: str
    workspace_root: Path
    session_path_resolver: SessionCatalogPathResolver


@pytest.fixture
def file_tree_settings(
    empty_workspace_scope: tuple[str, Path],
) -> FileTreeSettingsFixture:
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
    service = FileTreeSettingsService(
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
    return FileTreeSettingsFixture(
        service,
        shortcut_path,
        session_ids[0],
        session_ids[1],
        workspace_root,
        session_path_resolver,
    )


def test_session_shortcut_is_stored_inside_session_node(
    file_tree_settings: FileTreeSettingsFixture,
) -> None:
    service = file_tree_settings.service
    shortcut_path = file_tree_settings.shortcut_path
    first_session_id = file_tree_settings.first_session_id

    settings = service.add_session_shortcut(
        first_session_id,
        path=str(shortcut_path),
        label="Torch Home",
    )

    assert [(item.label, item.source) for item in settings.effective_shortcuts] == [
        ("Torch Home", "session")
    ]
    session_settings_path = (
        file_tree_settings.session_path_resolver.resolve_session_node(first_session_id)
        / "ui"
        / "file-tree-shortcuts.json"
    )
    assert session_settings_path.is_file()


def test_upgrade_snapshots_current_defaults_for_existing_sessions(
    file_tree_settings: FileTreeSettingsFixture,
) -> None:
    settings_path = (
        file_tree_settings.session_path_resolver.resolve_session_node(
            file_tree_settings.first_session_id
        )
        / "ui"
        / "file-tree-shortcuts.json"
    )
    settings_path.unlink()
    workspace_settings_path = (
        file_tree_settings.workspace_root
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
                        "path": file_tree_settings.service._file_paths.reference(file_tree_settings.shortcut_path),
                        "label": "Torch Home",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    migrated_service = FileTreeSettingsService(
        workspace_root=file_tree_settings.workspace_root,
        path_resolver=file_tree_settings.session_path_resolver,
        file_paths=file_tree_settings.service._file_paths,
    )

    settings = migrated_service.get(file_tree_settings.first_session_id)
    assert [(item.label, item.source) for item in settings.effective_shortcuts] == [
        ("Torch Home", "workspace")
    ]
    assert json.loads(settings_path.read_text(encoding="utf-8"))["version"] == 2


def test_default_shortcut_only_applies_to_sessions_created_after_change(
    file_tree_settings: FileTreeSettingsFixture,
) -> None:
    service = file_tree_settings.service
    shortcut_path = file_tree_settings.shortcut_path
    first_session_id = file_tree_settings.first_session_id
    second_session_id = file_tree_settings.second_session_id
    service.add_session_shortcut(
        first_session_id,
        path=str(shortcut_path),
        label="Torch Home",
    )

    promoted = service.apply_to_workspace(
        first_session_id,
        path=str(shortcut_path),
    )
    existing = service.get(second_session_id)

    third_session_id = "ses_00000000400070008000000000000013"
    seed_catalog_session_bundle(
        file_tree_settings.workspace_root / ".boxteam" / "sessions",
        third_session_id,
        title=third_session_id,
    )
    service.handle_session_change("create", third_session_id)
    inherited = service.get(third_session_id)

    assert promoted.effective_shortcuts[0].source == "session"
    assert existing.effective_shortcuts == []
    assert [(item.label, item.source) for item in inherited.effective_shortcuts] == [
        ("Torch Home", "workspace")
    ]
    assert [item.label for item in promoted.default_shortcuts] == ["Torch Home"]
    assert (
        file_tree_settings.workspace_root
        / ".boxteam"
        / "ui"
        / "file-tree-shortcuts.json"
    ).is_file()


def test_removing_default_does_not_change_existing_session_snapshots(
    file_tree_settings: FileTreeSettingsFixture,
) -> None:
    service = file_tree_settings.service
    shortcut_path = file_tree_settings.shortcut_path
    first_session_id = file_tree_settings.first_session_id
    service.add_session_shortcut(
        first_session_id,
        path=str(shortcut_path),
        label="Torch Home",
    )
    service.apply_to_workspace(first_session_id, path=str(shortcut_path))

    inherited_session_id = "ses_00000000400070008000000000000014"
    seed_catalog_session_bundle(
        file_tree_settings.workspace_root / ".boxteam" / "sessions",
        inherited_session_id,
        title=inherited_session_id,
    )
    service.initialize_session(inherited_session_id)

    service.remove_workspace_shortcut(
        first_session_id,
        path=str(shortcut_path),
    )

    assert service.get(first_session_id).effective_shortcuts[0].source == "session"
    inherited = service.get(inherited_session_id)
    assert inherited.effective_shortcuts[0].source == "workspace"
    assert inherited.default_shortcuts == []

    service.remove_session_shortcut(
        inherited_session_id,
        path=str(shortcut_path),
    )
    assert service.get(inherited_session_id).effective_shortcuts == []


def test_shortcut_rejects_relative_or_missing_directories(
    file_tree_settings: FileTreeSettingsFixture,
) -> None:
    service = file_tree_settings.service
    shortcut_path = file_tree_settings.shortcut_path
    first_session_id = file_tree_settings.first_session_id

    relative = service.add_session_shortcut(first_session_id, path="torch_home", label=None)
    assert relative.effective_shortcuts[0].path == service._file_paths.reference(shortcut_path)

    with pytest.raises(FileNotFoundError, match="不存在"):
        service.add_session_shortcut(
            first_session_id,
            path=str(shortcut_path / "missing"),
            label=None,
        )
