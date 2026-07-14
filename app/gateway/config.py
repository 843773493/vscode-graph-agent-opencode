from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from app.core.env import get_project_root
from app.services.infrastructure.config_service import ConfigService


@dataclass(frozen=True, slots=True)
class ConfiguredSshWorkspace:
    host: str
    username: str
    private_key_path: str
    remote_workspace_path: str
    kind: Literal["ssh"] = "ssh"
    name: str | None = None
    port: int = 22
    remote_backend_host: str = "127.0.0.1"
    remote_backend_port: int = 8010
    activate: bool = False


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    workspaces: tuple[ConfiguredSshWorkspace, ...] = ()


def _workspace_from_validated_config(raw: dict[str, object]) -> ConfiguredSshWorkspace:
    return ConfiguredSshWorkspace(
        name=cast(str | None, raw.get("name")),
        host=cast(str, raw["host"]),
        port=cast(int, raw.get("port", 22)),
        username=cast(str, raw["username"]),
        private_key_path=cast(str, raw["private_key_path"]),
        remote_backend_host=cast(str, raw.get("remote_backend_host", "127.0.0.1")),
        remote_backend_port=cast(int, raw.get("remote_backend_port", 8010)),
        remote_workspace_path=cast(str, raw["remote_workspace_path"]),
        activate=cast(bool, raw.get("activate", False)),
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
            _workspace_from_validated_config(item) for item in validated_workspaces
        )
    )


def resolve_gateway_path(value: str, *, project_root: Path | None = None) -> Path:
    raw_path = Path(value).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return ((project_root or get_project_root()) / raw_path).resolve()
