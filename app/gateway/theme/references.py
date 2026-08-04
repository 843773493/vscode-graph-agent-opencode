from pathlib import Path

from app.gateway.config import GatewayConfig
from app.gateway.schemas import WebUISettingsDTO
from app.gateway.theme.assets import import_ui_asset_file, update_ui_asset_references


def referenced_asset_ids(
    config: GatewayConfig,
    settings: WebUISettingsDTO,
    *,
    gateway_root: Path,
) -> dict[str, list[str]]:
    references: dict[str, list[str]] = {}
    candidates = [(theme.id, theme.background) for theme in config.custom_themes]
    candidates.append(
        (
            settings.theme.theme_id or "当前 UI 设置",
            settings.theme.background.model_dump()
            if settings.theme.background
            else None,
        )
    )
    for theme_id, background in candidates:
        if not background:
            continue
        if background.get("type") == "gateway_asset":
            asset_id = str(background["asset_id"])
        elif background.get("type") == "local_file":
            asset_id = import_ui_asset_file(
                Path(str(background["path"])), gateway_root=gateway_root
            ).asset_id
        else:
            continue
        references.setdefault(asset_id, []).append(theme_id)
    return references


def synchronize_theme_asset_references(
    config: GatewayConfig,
    settings: WebUISettingsDTO,
    *,
    gateway_root: Path,
) -> dict[str, list[str]]:
    references = referenced_asset_ids(
        config,
        settings,
        gateway_root=gateway_root,
    )
    update_ui_asset_references(references, gateway_root=gateway_root)
    return references
