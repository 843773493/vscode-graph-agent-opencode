from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from app.core.path_utils import get_user_config_path
from app.services.infrastructure.config_service import ConfigService


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
class GatewayConfig:
    workspaces: tuple[ConfiguredRemoteGateway, ...] = ()


def _workspace_from_validated_config(raw: dict[str, object]) -> ConfiguredRemoteGateway:
    legacy_fields = sorted(
        {
            "remote_workspace_path",
            "remote_backend_host",
            "remote_backend_port",
            "remote_terminal_backend_host",
            "remote_terminal_backend_port",
            "remote_browser_backend_host",
            "remote_browser_backend_port",
        }
        & raw.keys()
    )
    if legacy_fields or raw.get("kind") == "ssh":
        raise ValueError(
            "Gateway 配置仍使用已移除的 SSH 直连后端字段: "
            f"{', '.join(legacy_fields) or 'kind=ssh'}。"
            "请改为 kind=remote_gateway，并只配置 remote_gateway_port；"
            "远程工作区由远端 Gateway 自动发现。"
        )
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


def load_gateway_config(workspace_root: Path | None) -> GatewayConfig:
    if workspace_root is None:
        return GatewayConfig()

    config_service = ConfigService(workspace_root=workspace_root)
    config_service.validate_boxteam_config()
    raw_gateway_config = config_service.get_gateway_config()
    raw_workspaces = raw_gateway_config.get("workspaces", [])
    validated_workspaces = cast(list[dict[str, object]], raw_workspaces)
    return GatewayConfig(
        workspaces=tuple(
            workspace
            for item in validated_workspaces
            if cast(bool, item.get("enabled", True))
            if (workspace := _workspace_from_validated_config(item)).enabled
        )
    )


def resolve_gateway_path(value: str, *, config_root: Path | None = None) -> Path:
    raw_path = Path(value).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return ((config_root or get_user_config_path().parent) / raw_path).resolve()
