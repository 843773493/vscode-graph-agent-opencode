from __future__ import annotations

import json
import os
from pathlib import Path

from app.gateway.schemas import WebUISettingsDTO, WebUISettingsUpdateDTO


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
    return WebUISettingsDTO.model_validate(raw_settings)


def write_web_ui_settings(
    settings: WebUISettingsDTO,
    *,
    gateway_root: Path,
) -> WebUISettingsDTO:
    settings_path = web_ui_settings_path(gateway_root)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        settings.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return settings


def merge_web_ui_settings(
    payload: WebUISettingsUpdateDTO,
    *,
    gateway_root: Path,
) -> WebUISettingsDTO:
    current = read_web_ui_settings(gateway_root)
    data = current.model_dump()
    if payload.layout is not None:
        layout_patch = payload.layout.model_dump(exclude_unset=True)
        data["layout"] = {**data.get("layout", {}), **layout_patch}
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
    return write_web_ui_settings(
        WebUISettingsDTO.model_validate(data),
        gateway_root=gateway_root,
    )
