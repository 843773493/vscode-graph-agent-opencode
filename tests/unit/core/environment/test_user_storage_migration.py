from pathlib import Path

import pytest

from app.core.user_storage_migration import migrate_user_storage_layout


def test_migrate_user_storage_layout_moves_global_data(tmp_path, monkeypatch):
    home = tmp_path / "home"
    boxteam_home = home / ".boxteams"
    default_workspace = boxteam_home / "boxteam_workspace"
    monkeypatch.setenv("BOXTEAM_HOME", str(boxteam_home))
    legacy_config = home / ".boxteam"
    legacy_config.mkdir(parents=True)
    (legacy_config / "boxteam.jsonc").write_text("{}", encoding="utf-8")
    (legacy_config / "web_ui_settings.json").write_text("{}", encoding="utf-8")
    legacy_gateway = default_workspace / ".boxteam" / "gateway"
    legacy_gateway.mkdir(parents=True)
    (legacy_gateway / "workspaces.json").write_text("{}", encoding="utf-8")

    migrate_user_storage_layout(
        home=home,
        boxteam_home=boxteam_home,
        default_workspace_root=default_workspace,
    )

    assert (boxteam_home / "config" / "boxteam.jsonc").is_file()
    assert (boxteam_home / "state" / "gateway" / "workspaces.json").is_file()
    assert (
        boxteam_home / "state" / "migrated" / "legacy_web_ui_settings.json"
    ).is_file()
    assert not legacy_gateway.exists()


@pytest.mark.parametrize("preview", [True, False])
def test_storage_checks_late_target_conflict_before_moving_first_source(
    tmp_path: Path, preview: bool,
) -> None:
    home = tmp_path / "home"
    boxteam_home = home / ".boxteams"
    source = home / ".boxteam"
    source.mkdir(parents=True)
    (source / "boxteam.jsonc").write_bytes(b"first source")
    (source / "web_ui_settings.json").write_bytes(b"last source")
    target = boxteam_home / "state/migrated/legacy_web_ui_settings.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing data")
    before = {path.relative_to(home): path.read_bytes() if path.is_file() else None for path in home.rglob("*")}
    with pytest.raises(FileExistsError, match="迁移目标已存在"):
        migrate_user_storage_layout(
            home=home, boxteam_home=boxteam_home,
            default_workspace_root=boxteam_home / "boxteam_workspace", preview=preview,
        )
    assert {path.relative_to(home): path.read_bytes() if path.is_file() else None for path in home.rglob("*")} == before


def test_storage_preview_keeps_supported_source_in_place(tmp_path: Path) -> None:
    home = tmp_path / "home"
    boxteam_home = home / ".boxteams"
    source = home / ".boxteam/boxteam.jsonc"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original bytes")
    migrate_user_storage_layout(
        home=home, boxteam_home=boxteam_home,
        default_workspace_root=boxteam_home / "boxteam_workspace", preview=True,
    )
    assert source.read_bytes() == b"original bytes"
    assert not boxteam_home.exists()
