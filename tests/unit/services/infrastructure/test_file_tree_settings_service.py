from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pytest

from app.core.session_paths import SessionPathResolver
from app.services.infrastructure.file_tree_settings_service import (
    FileTreeSettingsService,
)


class FileTreeFixture(NamedTuple):
    service: FileTreeSettingsService
    shortcut_path: Path
    first_session_id: str
    second_session_id: str
    workspace_root: Path
    resolver: SessionPathResolver


@pytest.fixture
def file_tree_settings() -> FileTreeFixture:
    output_root = (
        Path.cwd()
        / "out/tests/unit/services/infrastructure/test_file_tree_settings_service"
    )
    if output_root.exists():
        shutil.rmtree(output_root)
    workspace_root = output_root / "workspace"
    sessions_root = workspace_root / ".boxteam" / "sessions"
    resolver = SessionPathResolver(sessions_root)
    resolver.initialize()
    session_ids = ("ses_shortcut_first", "ses_shortcut_second")
    now = datetime.now(UTC).isoformat()
    for session_id in session_ids:
        session_dir = resolver.allocate_session_dir(
            session_id=session_id,
            title=session_id,
            parent_node_id=None,
        )
        (session_dir / "session.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "title": session_id,
                    "created_at": now,
                    "updated_at": now,
                }
            ),
            encoding="utf-8",
        )
        resolver.register_session(session_id, session_dir)
    service = FileTreeSettingsService(
        workspace_root=workspace_root,
        path_resolver=resolver,
    )
    shortcut_path = workspace_root / "torch_home"
    shortcut_path.mkdir()
    return FileTreeFixture(
        service,
        shortcut_path,
        session_ids[0],
        session_ids[1],
        workspace_root,
        resolver,
    )


def test_session_shortcut_is_stored_inside_session_node(
    file_tree_settings: FileTreeFixture,
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
        file_tree_settings.resolver.resolve_session_node(first_session_id)
        / "ui"
        / "file-tree-shortcuts.json"
    )
    assert session_settings_path.is_file()


def test_upgrade_snapshots_current_defaults_for_existing_sessions(
    file_tree_settings: FileTreeFixture,
) -> None:
    settings_path = (
        file_tree_settings.resolver.resolve_session_node(
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
                        "path": str(file_tree_settings.shortcut_path),
                        "label": "Torch Home",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    migrated_service = FileTreeSettingsService(
        workspace_root=file_tree_settings.workspace_root,
        path_resolver=file_tree_settings.resolver,
    )

    settings = migrated_service.get(file_tree_settings.first_session_id)
    assert [(item.label, item.source) for item in settings.effective_shortcuts] == [
        ("Torch Home", "workspace")
    ]
    assert json.loads(settings_path.read_text(encoding="utf-8"))["version"] == 2


def test_default_shortcut_only_applies_to_sessions_created_after_change(
    file_tree_settings: FileTreeFixture,
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

    third_session_id = "ses_shortcut_third"
    third_session_dir = file_tree_settings.resolver.allocate_session_dir(
        session_id=third_session_id,
        title=third_session_id,
        parent_node_id=None,
    )
    now = datetime.now(UTC).isoformat()
    (third_session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": third_session_id,
                "title": third_session_id,
                "created_at": now,
                "updated_at": now,
            }
        ),
        encoding="utf-8",
    )
    file_tree_settings.resolver.register_session(third_session_id, third_session_dir)
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
    file_tree_settings: FileTreeFixture,
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

    inherited_session_id = "ses_shortcut_inherited"
    inherited_session_dir = file_tree_settings.resolver.allocate_session_dir(
        session_id=inherited_session_id,
        title=inherited_session_id,
        parent_node_id=None,
    )
    now = datetime.now(UTC).isoformat()
    (inherited_session_dir / "session.json").write_text(
        json.dumps(
            {
                "session_id": inherited_session_id,
                "title": inherited_session_id,
                "created_at": now,
                "updated_at": now,
            }
        ),
        encoding="utf-8",
    )
    file_tree_settings.resolver.register_session(
        inherited_session_id,
        inherited_session_dir,
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
    file_tree_settings: FileTreeFixture,
) -> None:
    service = file_tree_settings.service
    shortcut_path = file_tree_settings.shortcut_path
    first_session_id = file_tree_settings.first_session_id

    with pytest.raises(ValueError, match="绝对路径"):
        service.add_session_shortcut(
            first_session_id,
            path="torch_home",
            label=None,
        )
    with pytest.raises(FileNotFoundError, match="不存在"):
        service.add_session_shortcut(
            first_session_id,
            path=str(shortcut_path / "missing"),
            label=None,
        )
