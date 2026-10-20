from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.gateway.config import ConfiguredTheme, GatewayConfig
from app.gateway.control import user_profile as user_profile_module
from app.gateway.control.user_profile import UserProfileStore
from app.schemas.gateway.ui_settings import WebUISettingsDTO


@pytest.fixture
def user_profile_store(tmp_path: Path) -> UserProfileStore:
    store = UserProfileStore(gateway_root=tmp_path / "gateway")
    store.ensure_user(user_id="user-a", display_name="用户 A")
    return store


def test_user_profile_isolated_and_gitignored_runtime_data(tmp_path):
    store = UserProfileStore(gateway_root=tmp_path / "gateway")
    profile_root = store.ensure_user(user_id="user-a", display_name="用户 A")

    assert profile_root == tmp_path / "gateway" / "users" / "user-a"
    assert (profile_root / "themes").is_dir()
    assert "*.sqlite" in (profile_root / ".gitignore").read_text(encoding="utf-8")
    assert "workspaces/" in (profile_root / ".gitignore").read_text(encoding="utf-8")
    profile = json.loads((profile_root / "profile.jsonc").read_text(encoding="utf-8"))
    assert profile["config_version"] == 1
    assert profile["display_name"] == "用户 A"

    other = store.ensure_user(user_id="user-b", display_name="用户 B")
    assert other != profile_root
    assert store.read_profile(user_id="user-b")["display_name"] == "用户 B"


def test_user_profile_rejects_invalid_user_id(tmp_path):
    store = UserProfileStore(gateway_root=tmp_path / "gateway")
    with pytest.raises(ValueError, match="非法用户 ID"):
        store.ensure_user(user_id="../outside", display_name="越界")


def test_ui_settings_round_trip_does_not_store_native_directory_history(user_profile_store, tmp_path):
    settings = WebUISettingsDTO(layout={"workbench_view": "gateway"})
    user_profile_store.write_ui_settings(user_id="user-a", settings=settings)
    profile = user_profile_store.read_profile(user_id="user-a")
    assert set(profile["preferences"]) == {"session_sidebar", "workspace_file_tree", "gateway_console"}
    assert "recent_local_workspace_paths" not in settings.model_dump()
    reopened = UserProfileStore(gateway_root=tmp_path / "gateway")
    assert reopened.read_ui_settings(user_id="user-a") == settings


def test_file_preferences_round_trip_keeps_workspace_and_gateway_uris(user_profile_store, tmp_path):
    workspace = "boxteam://workspace/absent-workspace/resources/files/root"
    gateway = "boxteam://gateway/gateway_01900000000070008000000000000001/resources/files/01900000000070008000000000000001"
    uri = gateway + "/%E4%B8%AD%20%252F.txt"
    settings = WebUISettingsDTO(layout={"workspace_preview_file_paths": [uri],
        "workspace_preview_active_file_path": uri},
        workspace_file_tree={"expanded_paths_by_workspace": {"target": [workspace, gateway]}})
    user_profile_store.write_ui_settings(user_id="user-a", settings=settings)
    profile_path = user_profile_store.user_path("user-a") / "profile.jsonc"
    original = profile_path.read_bytes()
    assert str(tmp_path).encode() not in original
    reopened = UserProfileStore(gateway_root=tmp_path / "gateway")
    assert reopened.read_ui_settings(user_id="user-a") == settings
    assert profile_path.read_bytes() == original


@pytest.mark.parametrize("section,field,value", [
    ("layout", "workspace_preview_file_paths", ["/private/native.txt"]),
    ("layout", "workspace_preview_active_file_path", r"C:\private\native.txt"),
    ("preferences", "workspace_file_tree", {"expanded_paths_by_workspace": {"target": ["src"]}}),
])
def test_obsolete_file_preferences_fail_cold_read_and_write_without_rewriting(user_profile_store, section, field, value):
    profile_path = user_profile_store.user_path("user-a") / "profile.jsonc"
    profile = json.loads(profile_path.read_text())
    profile[section][field] = value
    original = json.dumps(profile).encode()
    profile_path.write_bytes(original)
    reopened = UserProfileStore(gateway_root=profile_path.parent.parent.parent)
    with pytest.raises(ValueError):
        reopened.read_ui_settings(user_id="user-a")
    with pytest.raises(ValueError):
        reopened.write_ui_settings(user_id="user-a", settings=WebUISettingsDTO())
    assert profile_path.read_bytes() == original


def test_user_profile_rejects_obsolete_native_directory_history_without_rewriting(user_profile_store):
    profile_path = user_profile_store.user_path("user-a") / "profile.jsonc"
    profile = json.loads(profile_path.read_text())
    profile["preferences"]["recent_local_workspace_paths"] = ["/private/native-directory"]
    original = json.dumps(profile).encode()
    profile_path.write_bytes(original)
    with pytest.raises(ValueError, match="recent_local_workspace_paths"):
        user_profile_store.read_ui_settings(user_id="user-a")
    with pytest.raises(ValueError, match="recent_local_workspace_paths"):
        user_profile_store.write_ui_settings(user_id="user-a", settings=WebUISettingsDTO())
    assert profile_path.read_bytes() == original


def test_custom_themes_are_seeded_only_into_first_user_profile(tmp_path):
    store = UserProfileStore(gateway_root=tmp_path / "gateway")
    config = GatewayConfig(
        default_theme_id="forest",
        custom_themes=(
            ConfiguredTheme(
                id="forest",
                label="森林",
                extends="green",
                color_scheme="light",
                tokens={"--bt-accent": "#123456"},
            ),
        ),
    )

    store.ensure_user(
        user_id="user-a",
        display_name="用户 A",
        initial_custom_themes=config.custom_themes,
    )
    store.ensure_user(
        user_id="user-b",
        display_name="用户 B",
        initial_custom_themes=config.custom_themes,
    )

    first_config = store.theme_config(user_id="user-a", base_config=config)
    second_config = store.theme_config(user_id="user-b", base_config=config)
    assert [theme.id for theme in first_config.custom_themes] == ["forest"]
    assert second_config.custom_themes == ()
    assert second_config.default_theme_id == "warm"


def test_user_profile_ignores_removed_global_ui_settings_file(tmp_path):
    gateway_root = tmp_path / "gateway"
    gateway_root.mkdir(parents=True)
    legacy_path = gateway_root / "web_ui_settings.json"
    legacy_path.write_text(
        json.dumps(
            {
                "layout": {"workbench_view": "gateway"},
                "theme": {"theme_id": "blue"},
            }
        ),
        encoding="utf-8",
    )
    store = UserProfileStore(gateway_root=gateway_root)
    store.ensure_user(user_id="user-a", display_name="用户 A")

    settings = store.read_ui_settings(user_id="user-a")
    assert settings.layout.workbench_view is None
    assert settings.theme.theme_id is None
    assert legacy_path.is_file()
    assert not (gateway_root / "web_ui_settings.json.migrated.bak").exists()


def test_user_profile_write_preserves_primary_error_when_temp_cleanup_fails(
    user_profile_store: UserProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = user_profile_store.user_path("user-a") / "profile.jsonc"
    original_bytes = profile_path.read_bytes()
    real_unlink = Path.unlink
    replace_error = OSError("原子替换失败")
    cleanup_error = OSError("临时文件清理失败")
    temporary_paths: list[Path] = []

    def fail_replace(source: Path, destination: Path) -> None:
        temporary_paths.append(source)
        raise replace_error

    def fail_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path in temporary_paths:
            raise cleanup_error
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(user_profile_module.os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    try:
        with pytest.raises(OSError) as caught:
            user_profile_store._write_profile(
                user_id="user-a",
                payload={"config_version": 1, "display_name": "新用户"},
            )

        assert caught.value is replace_error
        assert caught.value.__notes__ == [
            (
                "用户 profile 原子写入失败后临时文件清理失败: "
                "OSError: 临时文件清理失败"
            )
        ]
        assert profile_path.read_bytes() == original_bytes
        assert len(temporary_paths) == 1
        assert temporary_paths[0].is_file()
    finally:
        for temporary_path in profile_path.parent.glob(f".{profile_path.name}.*"):
            real_unlink(temporary_path, missing_ok=True)


def test_user_profile_write_keeps_primary_error_when_temp_cleanup_succeeds(
    user_profile_store: UserProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = user_profile_store.user_path("user-a") / "profile.jsonc"
    original_bytes = profile_path.read_bytes()
    real_unlink = Path.unlink
    replace_error = OSError("原子替换失败")
    temporary_paths: list[Path] = []

    def fail_replace(source: Path, destination: Path) -> None:
        temporary_paths.append(source)
        raise replace_error

    monkeypatch.setattr(user_profile_module.os, "replace", fail_replace)
    try:
        with pytest.raises(OSError) as caught:
            user_profile_store._write_profile(
                user_id="user-a",
                payload={"config_version": 1, "display_name": "新用户"},
            )

        assert caught.value is replace_error
        assert profile_path.read_bytes() == original_bytes
        assert len(temporary_paths) == 1
        assert not temporary_paths[0].exists()
        assert list(profile_path.parent.glob(f".{profile_path.name}.*")) == []
    finally:
        for temporary_path in profile_path.parent.glob(f".{profile_path.name}.*"):
            real_unlink(temporary_path, missing_ok=True)


def test_user_profile_write_replaces_file_and_reads_new_body(
    user_profile_store: UserProfileStore,
) -> None:
    profile_path = user_profile_store.user_path("user-a") / "profile.jsonc"
    payload = {
        "config_version": 1,
        "display_name": "更新后的用户",
        "theme": {"theme_id": "blue"},
    }

    user_profile_store._write_profile(user_id="user-a", payload=payload)

    assert user_profile_store.read_profile(user_id="user-a") == payload
    assert not list(profile_path.parent.glob(f".{profile_path.name}.*"))


def test_user_profile_write_reports_cleanup_error_after_commit(
    user_profile_store: UserProfileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = user_profile_store.user_path("user-a") / "profile.jsonc"
    original_bytes = profile_path.read_bytes()
    real_unlink = Path.unlink
    cleanup_error = OSError("提交后清理失败")
    temporary_paths: list[Path] = []
    payload = {"config_version": 1, "display_name": "已经提交"}

    def fail_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.parent == profile_path.parent and path.name.startswith(
            f".{profile_path.name}."
        ):
            temporary_paths.append(path)
            raise cleanup_error
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    try:
        with pytest.raises(OSError) as caught:
            user_profile_store._write_profile(user_id="user-a", payload=payload)

        assert caught.value is cleanup_error
        assert profile_path.read_bytes() != original_bytes
        assert user_profile_store.read_profile(user_id="user-a") == payload
        assert len(temporary_paths) == 1
        assert not temporary_paths[0].exists()
    finally:
        for temporary_path in profile_path.parent.glob(f".{profile_path.name}.*"):
            real_unlink(temporary_path, missing_ok=True)
