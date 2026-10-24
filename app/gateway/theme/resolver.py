import logging
from pathlib import Path
from urllib.parse import urlparse

from app.gateway.config import ConfiguredTheme, GatewayConfig
from app.gateway.theme.assets import import_ui_asset_file, resolve_ui_asset
from app.gateway.theme.builtins import VARIANTS, WARM_TOKENS
from app.gateway.theme.defaults import DEFAULT_THEME_BACKGROUND_OVERLAY
from app.gateway.theme.immersive import IMMERSIVE_TOKENS
from app.gateway.theme.models import ThemeDefinition
from app.gateway.theme.validation import (
    validate_background_display_tokens,
    validate_token,
)
from app.schemas.gateway.ui_settings import (
    GatewayThemeBackgroundDTO,
    GatewayThemeCatalogDTO,
    GatewayThemeOptionDTO,
    ResolvedGatewayThemeDTO,
    WebUISettingsDTO,
)

logger = logging.getLogger(__name__)

def theme_definitions(config: GatewayConfig) -> dict[str, ThemeDefinition]:
    definitions = {
        theme_id: ThemeDefinition(
            theme_id, label, theme_id, "light", {}, None, "builtin"
        )
        for theme_id, label in (("warm", "暖色"), ("green", "绿色"), ("blue", "蓝色"))
    }
    for custom in config.custom_themes:
        if custom.id in definitions:
            raise ValueError(f"自定义主题 ID 与内置主题重复: {custom.id}")
        for token_name, token_value in custom.tokens.items():
            validate_token(token_name, token_value)
        definitions[custom.id] = _from_config(custom)
    if config.default_theme_id not in definitions:
        raise ValueError(f"Gateway 默认主题不存在: {config.default_theme_id}")
    return definitions


def _from_config(theme: ConfiguredTheme) -> ThemeDefinition:
    return ThemeDefinition(
        theme.id,
        theme.label,
        theme.extends,
        theme.color_scheme,
        theme.tokens,
        theme.background,
        "gateway_config",
    )


def _background(
    background_config: dict[str, object] | GatewayThemeBackgroundDTO | None,
    *,
    gateway_root: Path,
) -> tuple[str | None, dict[str, str], str]:
    if background_config is None:
        return None, {}, "theme"
    background_data = (
        background_config.model_dump()
        if isinstance(background_config, GatewayThemeBackgroundDTO)
        else background_config
    )
    kind = str(background_data["type"])
    if kind == "remote":
        url = str(background_data["url"])
        scheme = urlparse(url).scheme
        if scheme not in {"http", "https"}:
            logger.error("网络背景只支持 http/https URL: url=%s", url)
            raise ValueError(f"网络背景只支持 http/https URL: scheme={scheme or '<empty>'}")
    elif kind == "local_file":
        asset = import_ui_asset_file(
            Path(str(background_data["path"])), gateway_root=gateway_root
        )
        url = asset.url
    elif kind == "gateway_asset":
        asset_id = str(background_data["asset_id"])
        resolve_ui_asset(asset_id, gateway_root=gateway_root)
        url = f"/api/gateway/ui-assets/{asset_id}"
    else:
        raise ValueError(f"不支持的主题背景类型: {kind}")
    display_tokens = {
        "--bt-background-position": str(background_data.get("position", "center")),
        "--bt-background-size": str(background_data.get("size", "cover")),
        "--bt-background-repeat": str(background_data.get("repeat", "no-repeat")),
        "--bt-background-overlay": str(
            background_data.get("overlay", DEFAULT_THEME_BACKGROUND_OVERLAY)
        ),
    }
    validate_background_display_tokens(display_tokens)
    appearance = str(background_data.get("appearance", "immersive"))
    if appearance not in {"immersive", "theme"}:
        raise ValueError(f"不支持的背景外观: {appearance}")
    return url, display_tokens, appearance


def _resolve_theme_from_definitions(
    theme_id: str,
    *,
    definitions: dict[str, ThemeDefinition],
    gateway_root: Path,
    background_override: GatewayThemeBackgroundDTO | None = None,
) -> ResolvedGatewayThemeDTO:
    definition = definitions.get(theme_id)
    if definition is None:
        raise ValueError(f"Gateway 主题不存在: {theme_id}")
    tokens = {**WARM_TOKENS, **VARIANTS[definition.extends], **definition.tokens}
    background_url, background_tokens, background_appearance = _background(
        background_override
        if background_override is not None
        else definition.background,
        gateway_root=gateway_root,
    )
    if background_url is not None and background_appearance == "immersive":
        tokens.update(IMMERSIVE_TOKENS)
    tokens.update(background_tokens)
    return ResolvedGatewayThemeDTO(
        id=definition.id,
        label=definition.label,
        color_scheme=(
            "dark"
            if background_url is not None and background_appearance == "immersive"
            else definition.color_scheme
        ),
        tokens=tokens,
        background_image_url=background_url,
    )


def resolve_theme(
    theme_id: str,
    *,
    config: GatewayConfig,
    gateway_root: Path,
    background_override: GatewayThemeBackgroundDTO | None = None,
) -> ResolvedGatewayThemeDTO:
    return _resolve_theme_from_definitions(
        theme_id,
        definitions=theme_definitions(config),
        gateway_root=gateway_root,
        background_override=background_override,
    )


def _resolve_settings_theme_from_definitions(
    settings: WebUISettingsDTO,
    *,
    config: GatewayConfig,
    definitions: dict[str, ThemeDefinition],
    gateway_root: Path,
) -> WebUISettingsDTO:
    selected_id = settings.theme.theme_id or config.default_theme_id
    resolved = _resolve_theme_from_definitions(
        selected_id,
        definitions=definitions,
        gateway_root=gateway_root,
        background_override=settings.theme.background,
    )
    return settings.model_copy(
        update={
            "theme": settings.theme.model_copy(
                update={"theme_id": selected_id, "resolved_theme": resolved}
            )
        }
    )


def resolve_settings_theme(
    settings: WebUISettingsDTO,
    *,
    config: GatewayConfig,
    gateway_root: Path,
) -> WebUISettingsDTO:
    return _resolve_settings_theme_from_definitions(
        settings,
        config=config,
        definitions=theme_definitions(config),
        gateway_root=gateway_root,
    )


def theme_catalog(
    settings: WebUISettingsDTO, *, config: GatewayConfig, gateway_root: Path
) -> GatewayThemeCatalogDTO:
    definitions = theme_definitions(config)
    resolved_options = {
        theme_id: _resolve_theme_from_definitions(
            theme_id,
            definitions=definitions,
            gateway_root=gateway_root,
        )
        for theme_id in definitions
    }
    selected_theme_id = settings.theme.theme_id or config.default_theme_id
    if selected_theme_id not in definitions:
        raise ValueError(f"Gateway 主题不存在: {selected_theme_id}")
    current_theme = (
        resolved_options[selected_theme_id]
        if settings.theme.background is None
        else _resolve_theme_from_definitions(
            selected_theme_id,
            definitions=definitions,
            gateway_root=gateway_root,
            background_override=settings.theme.background,
        )
    )
    theme_options = []
    for definition in definitions.values():
        resolved_option = resolved_options[definition.id]
        tokens = resolved_option.tokens
        theme_options.append(
            GatewayThemeOptionDTO(
                id=definition.id,
                label=definition.label,
                extends=definition.extends,
                source=definition.source,
                preview_tokens={
                    name: tokens[name]
                    for name in (
                        "--bt-page-background",
                        "--bt-panel-background",
                        "--bt-accent",
                    )
                },
                background_image_url=resolved_option.background_image_url,
            )
        )
    return GatewayThemeCatalogDTO(
        current_theme_id=current_theme.id,
        items=theme_options,
        current_theme=current_theme,
    )
