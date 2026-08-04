import hashlib
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from app.core.path_utils import get_user_gateway_config_path
from app.gateway.config import GatewayConfig, load_gateway_config
from app.gateway.theme.resolver import resolve_theme, theme_definitions


@dataclass(frozen=True, slots=True)
class ThemeConfigSnapshot:
    revision: str
    config: GatewayConfig


_THEME_CONFIG_SNAPSHOTS: dict[Path, ThemeConfigSnapshot] = {}
_THEME_CONFIG_LOCK = RLock()


def load_validated_theme_config(*, gateway_root: Path) -> GatewayConfig:
    config_path = get_user_gateway_config_path()
    with _THEME_CONFIG_LOCK:
        cached = _THEME_CONFIG_SNAPSHOTS.get(config_path)
        try:
            revision = hashlib.sha256(config_path.read_bytes()).hexdigest()
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
