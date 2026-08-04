__all__ = [
    "MAX_UI_ASSET_BYTES",
    "delete_ui_asset",
    "import_ui_asset",
    "list_ui_assets",
    "load_validated_theme_config",
    "referenced_asset_ids",
    "resolve_settings_theme",
    "resolve_theme",
    "resolve_ui_asset",
    "synchronize_theme_asset_references",
    "theme_catalog",
]


def __getattr__(name: str) -> object:
    if name in {
        "MAX_UI_ASSET_BYTES",
        "delete_ui_asset",
        "import_ui_asset",
        "list_ui_assets",
        "resolve_ui_asset",
    }:
        from app.gateway.theme import assets

        return getattr(assets, name)
    if name == "load_validated_theme_config":
        from app.gateway.theme.config_store import load_validated_theme_config

        return load_validated_theme_config
    if name in {"referenced_asset_ids", "synchronize_theme_asset_references"}:
        from app.gateway.theme import references

        return getattr(references, name)
    if name in {"resolve_settings_theme", "resolve_theme", "theme_catalog"}:
        from app.gateway.theme import resolver

        return getattr(resolver, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
