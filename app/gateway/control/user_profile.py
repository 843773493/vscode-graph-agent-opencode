from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from app.gateway.control.user_access import USER_ID_PATTERN
from app.gateway.config import ConfiguredTheme, GatewayConfig
from app.schemas.gateway import WebUISettingsDTO, WebUISettingsUpdateDTO
from app.gateway.ui_settings import (
    merge_web_ui_settings_values,
    read_web_ui_settings,
)

_PROFILE_VERSION = 1
_PROFILE_GITIGNORE = """# 由 BoxTeam 管理；个人主题和 profile.jsonc 可以提交到用户自己的 Git 仓库。
*.sqlite
*.sqlite-*
credentials/
connections/
workspaces/
sessions/
cache/
runtime/
*_local.jsonc
"""


class UserProfileStore:
    def __init__(self, *, gateway_root: Path) -> None:
        self._gateway_root = gateway_root.expanduser().resolve()
        self._users_root = self._gateway_root / "users"

    def user_path(self, user_id: str) -> Path:
        if not USER_ID_PATTERN.fullmatch(user_id):
            raise ValueError(f"非法用户 ID，不能解析 profile 路径: {user_id}")
        return self._users_root / user_id

    def theme_assets_path(self, *, user_id: str) -> Path:
        """返回用户主题资源目录，并确保它位于用户 profile 内。"""
        profile_root = self.user_path(user_id)
        assets_root = profile_root / "themes" / "assets"
        assets_root.mkdir(parents=True, exist_ok=True)
        return assets_root

    def ensure_user(
        self,
        *,
        user_id: str,
        display_name: str,
        initial_custom_themes: tuple[ConfiguredTheme, ...] = (),
    ) -> Path:
        profile_root = self.user_path(user_id)
        first_user_profile = not any(
            path.is_dir() for path in self._users_root.glob("*")
        )
        profile_root.mkdir(parents=True, exist_ok=True)
        (profile_root / "themes").mkdir(exist_ok=True)
        gitignore_path = profile_root / ".gitignore"
        if not gitignore_path.exists():
            _atomic_write_text(gitignore_path, _PROFILE_GITIGNORE)
        profile_path = profile_root / "profile.jsonc"
        if profile_path.exists():
            self._read_profile(profile_path)
        else:
            _atomic_write_text(
                profile_path,
                json.dumps(
                    {
                        "$schema": "user_profile_schema.jsonc",
                        "config_version": _PROFILE_VERSION,
                        "display_name": display_name,
                        "theme": {},
                        "layout": {},
                        "preferences": {},
                        "custom_themes": (
                            [asdict(theme) for theme in initial_custom_themes]
                            if first_user_profile
                            else []
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
        self._migrate_legacy_ui_settings(user_id=user_id)
        return profile_root

    def theme_config(
        self,
        *,
        user_id: str,
        base_config: GatewayConfig,
    ) -> GatewayConfig:
        profile = self.read_profile(user_id=user_id)
        raw_themes = profile.get("custom_themes", [])
        if not isinstance(raw_themes, list):
            raise ValueError(f"用户 profile.custom_themes 必须是数组: user_id={user_id}")
        custom_themes = tuple(
            _profile_theme_from_dict(item, user_id=user_id, index=index)
            for index, item in enumerate(raw_themes)
        )
        builtin_ids = {"warm", "green", "blue"}
        custom_ids = {theme.id for theme in custom_themes}
        default_theme_id = (
            base_config.default_theme_id
            if base_config.default_theme_id in builtin_ids | custom_ids
            else "warm"
        )
        return replace(
            base_config,
            default_theme_id=default_theme_id,
            custom_themes=custom_themes,
        )

    @staticmethod
    def guest_theme_config(base_config: GatewayConfig) -> GatewayConfig:
        return replace(
            base_config,
            default_theme_id="warm",
            custom_themes=(),
        )

    def read_profile(self, *, user_id: str) -> dict[str, object]:
        return self._read_profile(self.user_path(user_id) / "profile.jsonc")

    def delete_user(self, *, user_id: str) -> None:
        profile_root = self.user_path(user_id)
        if not profile_root.exists():
            return
        if not profile_root.is_dir():
            raise OSError(f"用户 profile 路径不是目录: {profile_root}")
        shutil.rmtree(profile_root)

    def read_ui_settings(self, *, user_id: str) -> WebUISettingsDTO:
        profile = self.read_profile(user_id=user_id)
        preferences = profile.get("preferences", {})
        if not isinstance(preferences, dict):
            raise ValueError(f"用户 profile.preferences 必须是对象: user_id={user_id}")
        return WebUISettingsDTO.model_validate(
            {
                "theme": profile.get("theme", {}),
                "layout": profile.get("layout", {}),
                "session_sidebar": preferences.get("session_sidebar", {}),
                "workspace_file_tree": preferences.get("workspace_file_tree", {}),
                "gateway_console": preferences.get("gateway_console", {}),
                "recent_local_workspace_paths": preferences.get(
                    "recent_local_workspace_paths", []
                ),
            }
        )

    def merge_ui_settings(
        self,
        *,
        user_id: str,
        payload: WebUISettingsUpdateDTO,
    ) -> WebUISettingsDTO:
        updated = merge_web_ui_settings_values(
            self.read_ui_settings(user_id=user_id),
            payload,
        )
        self.write_ui_settings(user_id=user_id, settings=updated)
        return updated

    def write_ui_settings(
        self,
        *,
        user_id: str,
        settings: WebUISettingsDTO,
    ) -> None:
        profile = self.read_profile(user_id=user_id)
        serialized = settings.model_dump(
            mode="json",
            exclude={"theme": {"resolved_theme"}},
        )
        profile["theme"] = serialized.pop("theme", {})
        profile["layout"] = serialized.pop("layout", {})
        preferences = profile.get("preferences", {})
        if not isinstance(preferences, dict):
            raise ValueError(f"用户 profile.preferences 必须是对象: user_id={user_id}")
        profile["preferences"] = {
            **preferences,
            "session_sidebar": serialized.pop("session_sidebar", {}),
            "workspace_file_tree": serialized.pop("workspace_file_tree", {}),
            "gateway_console": serialized.pop("gateway_console", {}),
            "recent_local_workspace_paths": serialized.pop(
                "recent_local_workspace_paths", []
            ),
        }
        self._write_profile(user_id=user_id, payload=profile)

    def _migrate_legacy_ui_settings(self, *, user_id: str) -> None:
        legacy_path = self._gateway_root / "web_ui_settings.json"
        if not legacy_path.is_file():
            return
        backup_path = legacy_path.with_name("web_ui_settings.json.migrated.bak")
        if backup_path.exists():
            return
        profile = self.read_profile(user_id=user_id)
        if any(profile.get(key) for key in ("theme", "layout", "preferences")):
            shutil.copy2(legacy_path, backup_path)
            return
        settings = read_web_ui_settings(self._gateway_root)
        self.write_ui_settings(user_id=user_id, settings=settings)
        shutil.copy2(legacy_path, backup_path)

    def _write_profile(self, *, user_id: str, payload: dict[str, object]) -> None:
        profile_path = self.user_path(user_id) / "profile.jsonc"
        _atomic_write_text(
            profile_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    @staticmethod
    def _read_profile(path: Path) -> dict[str, object]:
        if not path.is_file():
            raise FileNotFoundError(f"用户 profile 不存在: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"用户 profile 必须是 JSON 对象: {path}")
        if payload.get("config_version") != _PROFILE_VERSION:
            raise ValueError(f"用户 profile 版本不受支持: {path}")
        return payload


def _atomic_write_text(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if os.name != "nt":
            temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _profile_theme_from_dict(
    value: object,
    *,
    user_id: str,
    index: int,
) -> ConfiguredTheme:
    if not isinstance(value, dict):
        raise ValueError(
            f"用户 profile.custom_themes[{index}] 必须是对象: user_id={user_id}"
        )
    theme_id = value.get("id")
    label = value.get("label")
    extends = value.get("extends")
    color_scheme = value.get("color_scheme", "light")
    tokens = value.get("tokens", {})
    background = value.get("background")
    if (
        not isinstance(theme_id, str)
        or not USER_ID_PATTERN.fullmatch(theme_id)
        or not isinstance(label, str)
        or not label
        or extends not in {"warm", "green", "blue"}
        or color_scheme not in {"light", "dark"}
        or not isinstance(tokens, dict)
        or not all(isinstance(name, str) and isinstance(token, str) for name, token in tokens.items())
        or (background is not None and not isinstance(background, dict))
    ):
        raise ValueError(
            f"用户 profile.custom_themes[{index}] 结构非法: user_id={user_id}"
        )
    return ConfiguredTheme(
        id=theme_id,
        label=label,
        extends=extends,
        color_scheme=color_scheme,
        tokens=dict(tokens),
        background=dict(background) if background is not None else None,
    )
