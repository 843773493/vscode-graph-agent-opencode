from __future__ import annotations

import json

import pytest

from app.gateway.control.user_profile import UserProfileStore
from app.gateway.config import ConfiguredTheme, GatewayConfig
from app.schemas.gateway import WebUISettingsUpdateDTO


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


def test_user_profile_migrates_legacy_ui_settings_once(tmp_path):
    gateway_root = tmp_path / "gateway"
    (gateway_root).mkdir(parents=True)
    (gateway_root / "web_ui_settings.json").write_text(
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
    assert settings.layout.workbench_view == "gateway"
    assert settings.theme.theme_id == "blue"
    assert (gateway_root / "web_ui_settings.json.migrated.bak").is_file()

    store.merge_ui_settings(
        user_id="user-a",
        payload=WebUISettingsUpdateDTO(theme={"theme_id": "green"}),
    )
    assert store.read_ui_settings(user_id="user-a").theme.theme_id == "green"

    store.ensure_user(user_id="user-b", display_name="用户 B")
    assert store.read_ui_settings(user_id="user-b").theme.theme_id is None
    assert store.read_ui_settings(user_id="user-b").layout.workbench_view is None
