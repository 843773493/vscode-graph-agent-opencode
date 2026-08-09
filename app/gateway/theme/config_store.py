import hashlib
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from app.core.path_utils import (
    get_user_gateway_config_path,
    get_user_gateway_local_config_path,
)
from app.gateway.config import GatewayConfig, load_gateway_config
from app.gateway.theme.resolver import resolve_theme, theme_definitions
from configs.installer import resolve_config_resource_source


@dataclass(frozen=True, slots=True)
class ThemeConfigSnapshot:
    revision: str
    config: GatewayConfig


_THEME_CONFIG_SNAPSHOTS: dict[Path, ThemeConfigSnapshot] = {}
_THEME_CONFIG_LOCK = RLock()


def _gateway_config_file_revision() -> str:
    digest = hashlib.sha256()
    for path in (
        resolve_config_resource_source("gateway_inline.jsonc"),
        get_user_gateway_config_path(),
        get_user_gateway_local_config_path(),
    ):
        digest.update(str(path).encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


def load_validated_theme_config(*, gateway_root: Path) -> GatewayConfig:
    config_path = get_user_gateway_config_path()
    with _THEME_CONFIG_LOCK:
        cached = _THEME_CONFIG_SNAPSHOTS.get(config_path)
        try:
            revision = _gateway_config_file_revision()
            if cached is not None and cached.revision == revision:
                return cached.config
            config = load_gateway_config()
            definitions = theme_definitions(config)
            for theme_id in definitions:
                resolve_theme(theme_id, config=config, gateway_root=gateway_root)
        except (FileNotFoundError, TypeError, ValueError) as error:
            retained = "；已保留上一份有效主题配置" if cached is not None else ""
            raise ValueError(f"Gateway 主题配置重载失败{retained}: {error}") from error
        _THEME_CONFIG_SNAPSHOTS[config_path] = ThemeConfigSnapshot(
            revision=revision,
            config=config,
        )
        return config
