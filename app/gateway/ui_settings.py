from __future__ import annotations

from app.schemas.gateway.ui_settings import WebUISettingsDTO, WebUISettingsUpdateDTO


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
    return WebUISettingsDTO.model_validate(data)
