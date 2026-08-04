from pathlib import Path
from urllib.parse import urlparse

from app.gateway.config import ConfiguredTheme, GatewayConfig
from app.gateway.schemas import (
    GatewayThemeBackgroundDTO,
    GatewayThemeCatalogDTO,
    GatewayThemeOptionDTO,
    ResolvedGatewayThemeDTO,
    WebUISettingsDTO,
)
from app.gateway.theme.assets import import_ui_asset_file, resolve_ui_asset
from app.gateway.theme.builtins import VARIANTS, WARM_TOKENS
from app.gateway.theme.defaults import DEFAULT_THEME_BACKGROUND_OVERLAY
from app.gateway.theme.immersive import IMMERSIVE_TOKENS
from app.gateway.theme.models import ThemeDefinition
from app.gateway.theme.validation import (
    validate_background_display_tokens,
    validate_token,
)


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
        for name, value in custom.tokens.items():
            validate_token(name, value)
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
    raw: dict[str, object] | GatewayThemeBackgroundDTO | None,
    *,
    gateway_root: Path,
) -> tuple[str | None, dict[str, str], str]:
    if raw is None:
        return None, {}, "theme"
    data = raw.model_dump() if isinstance(raw, GatewayThemeBackgroundDTO) else raw
    kind = str(data["type"])
    if kind == "remote":
        url = str(data["url"])
        if urlparse(url).scheme not in {"http", "https"}:
            raise ValueError(f"网络背景只支持 http/https URL: {url}")
    elif kind == "local_file":
        asset = import_ui_asset_file(Path(str(data["path"])), gateway_root=gateway_root)
        url = asset.url
    elif kind == "gateway_asset":
        asset_id = str(data["asset_id"])
        resolve_ui_asset(asset_id, gateway_root=gateway_root)
        url = f"/api/gateway/ui-assets/{asset_id}"
    else:
        raise ValueError(f"不支持的主题背景类型: {kind}")
    display_tokens = {
        "--bt-background-position": str(data.get("position", "center")),
        "--bt-background-size": str(data.get("size", "cover")),
        "--bt-background-repeat": str(data.get("repeat", "no-repeat")),
        "--bt-background-overlay": str(
            data.get("overlay", DEFAULT_THEME_BACKGROUND_OVERLAY)
        ),
    }
    validate_background_display_tokens(display_tokens)
    appearance = str(data.get("appearance", "immersive"))
    if appearance not in {"immersive", "theme"}:
        raise ValueError(f"不支持的背景外观: {appearance}")
    return url, display_tokens, appearance


def resolve_theme(
    theme_id: str,
    *,
    config: GatewayConfig,
    gateway_root: Path,
    background_override: GatewayThemeBackgroundDTO | None = None,
) -> ResolvedGatewayThemeDTO:
    definitions = theme_definitions(config)
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


def resolve_settings_theme(
    settings: WebUISettingsDTO,
    *,
    config: GatewayConfig,
    gateway_root: Path,
) -> WebUISettingsDTO:
    selected_id = settings.theme.theme_id or config.default_theme_id
    resolved = resolve_theme(
        selected_id,
        config=config,
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


def theme_catalog(
    settings: WebUISettingsDTO, *, config: GatewayConfig, gateway_root: Path
) -> GatewayThemeCatalogDTO:
    resolved_settings = resolve_settings_theme(
        settings, config=config, gateway_root=gateway_root
    )
    current = resolved_settings.theme.resolved_theme
    if current is None:
        raise RuntimeError("主题解析器未返回当前主题")
    definitions = theme_definitions(config)
    items = []
    for definition in definitions.values():
        resolved_option = resolve_theme(
            definition.id,
            config=config,
            gateway_root=gateway_root,
        )
        tokens = resolved_option.tokens
        items.append(
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
        current_theme_id=current.id, items=items, current_theme=current
    )
