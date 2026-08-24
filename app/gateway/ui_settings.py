from __future__ import annotations

import json
import os
from pathlib import Path

from app.schemas.gateway import WebUISettingsDTO, WebUISettingsUpdateDTO


def web_ui_settings_path(gateway_root: Path) -> Path:
    configured = os.environ.get("BOXTEAM_WEB_UI_SETTINGS_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return gateway_root / "web_ui_settings.json"


def read_web_ui_settings(gateway_root: Path) -> WebUISettingsDTO:
    settings_path = web_ui_settings_path(gateway_root)
    if not settings_path.exists():
        return WebUISettingsDTO()
    raw_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    layout = raw_settings.get("layout")
    if isinstance(layout, dict) and "collapsed_workspace_ids" in layout:
        session_sidebar = raw_settings.setdefault("session_sidebar", {})
        if "collapsed_workspace_ids" not in session_sidebar:
            session_sidebar["collapsed_workspace_ids"] = layout[
                "collapsed_workspace_ids"
            ]
        del layout["collapsed_workspace_ids"]
    return WebUISettingsDTO.model_validate(raw_settings)


def write_web_ui_settings(
    settings: WebUISettingsDTO,
    *,
    gateway_root: Path,
) -> WebUISettingsDTO:
    settings_path = web_ui_settings_path(gateway_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = settings_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        settings.model_dump_json(
            indent=2,
            exclude={"theme": {"resolved_theme"}},
        ),
        encoding="utf-8",
    )
    temporary_path.replace(settings_path)
    return settings


def merge_web_ui_settings(
    payload: WebUISettingsUpdateDTO,
    *,
    gateway_root: Path,
) -> WebUISettingsDTO:
    current = read_web_ui_settings(gateway_root)
    updated = merge_web_ui_settings_values(current, payload)
    return write_web_ui_settings(updated, gateway_root=gateway_root)


def merge_web_ui_settings_values(
    current: WebUISettingsDTO,
    payload: WebUISettingsUpdateDTO,
) -> WebUISettingsDTO:
    data = current.model_dump()
    if payload.layout is not None:
        layout_patch = payload.layout.model_dump(exclude_unset=True)
        data["layout"] = {**data.get("layout", {}), **layout_patch}
    for section_name in ("session_sidebar", "workspace_file_tree", "gateway_console"):
        section = getattr(payload, section_name)
        if section is not None:
            section_patch = section.model_dump(exclude_unset=True)
            data[section_name] = {**data.get(section_name, {}), **section_patch}
    if payload.theme is not None:
        theme_patch = payload.theme.model_dump(exclude_unset=True)
        data["theme"] = {**data.get("theme", {}), **theme_patch}
    if payload.recent_local_workspace_paths is not None:
        seen_paths: set[str] = set()
        recent_paths: list[str] = []
        for raw_path in payload.recent_local_workspace_paths:
            path = raw_path.strip()
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            recent_paths.append(path)
        data["recent_local_workspace_paths"] = recent_paths[:20]
    return WebUISettingsDTO.model_validate(data)
