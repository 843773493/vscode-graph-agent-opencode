from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from app.core.path_utils import (
    get_user_config_root,
    get_user_gateway_config_path,
    get_user_gateway_schema_path,
)
from configs.runtime import load_validated_config


@dataclass(frozen=True, slots=True)
class ConfiguredRemoteGateway:
    host: str
    username: str
    private_key_path: str
    kind: Literal["remote_gateway"] = "remote_gateway"
    name: str | None = None
    port: int = 22
    remote_gateway_port: int = 8014
    activate: bool = False
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ConfiguredTheme:
    id: str
    label: str
    extends: Literal["warm", "green", "blue"]
    color_scheme: Literal["light", "dark"]
    tokens: dict[str, str]
    background: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    workspaces: tuple[ConfiguredRemoteGateway, ...] = ()
    default_theme_id: str = "warm"
    custom_themes: tuple[ConfiguredTheme, ...] = ()


def _workspace_from_validated_config(raw: dict[str, object]) -> ConfiguredRemoteGateway:
    return ConfiguredRemoteGateway(
        name=cast(str | None, raw.get("name")),
        host=cast(str, raw["host"]),
        port=cast(int, raw.get("port", 22)),
        username=cast(str, raw["username"]),
        private_key_path=cast(str, raw["private_key_path"]),
        remote_gateway_port=cast(int, raw.get("remote_gateway_port", 8014)),
        activate=cast(bool, raw.get("activate", False)),
        enabled=cast(bool, raw.get("enabled", True)),
    )


def load_gateway_config(
    *,
    config_path: Path | None = None,
    schema_path: Path | None = None,
) -> GatewayConfig:
    """只从 BOXTEAM_HOME 的 Gateway 配置域加载远程 Gateway。"""
    resolved_config_path = config_path or get_user_gateway_config_path()
    raw_gateway_config = load_validated_config(
        config_path=resolved_config_path,
        schema_path=schema_path or get_user_gateway_schema_path(),
    )
    raw_workspaces = raw_gateway_config["workspaces"]
    if not isinstance(raw_workspaces, list):
        raise TypeError("Gateway workspaces 配置必须是数组")
    validated_workspaces = cast(list[dict[str, object]], raw_workspaces)
    raw_ui = cast(dict[str, object], raw_gateway_config.get("ui", {}))
    raw_theme = cast(dict[str, object], raw_ui.get("theme", {}))
    raw_custom_themes = cast(
        list[dict[str, object]], raw_theme.get("custom_themes", [])
    )
    return GatewayConfig(
        workspaces=tuple(
            workspace
            for item in validated_workspaces
            if cast(bool, item.get("enabled", True))
            if (workspace := _workspace_from_validated_config(item)).enabled
        ),
        default_theme_id=cast(str, raw_theme.get("default_theme_id", "warm")),
        custom_themes=tuple(
            ConfiguredTheme(
                id=cast(str, item["id"]),
                label=cast(str, item["label"]),
                extends=cast(Literal["warm", "green", "blue"], item["extends"]),
                color_scheme=cast(
                    Literal["light", "dark"], item.get("color_scheme", "light")
                ),
                tokens=cast(dict[str, str], item.get("tokens", {})),
                background=_configured_theme_background(
                    cast(dict[str, object] | None, item.get("background")),
                    config_root=resolved_config_path.parent,
                ),
            )
            for item in raw_custom_themes
        ),
    )


def _configured_theme_background(
    background: dict[str, object] | None,
    *,
    config_root: Path,
) -> dict[str, object] | None:
    if background is None or background.get("type") != "local_file":
        return background
    return {
        **background,
        "path": str(
            resolve_gateway_path(cast(str, background["path"]), config_root=config_root)
        ),
    }


def resolve_gateway_path(value: str, *, config_root: Path | None = None) -> Path:
    raw_path = Path(value).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return ((config_root or get_user_config_root()) / raw_path).resolve()
