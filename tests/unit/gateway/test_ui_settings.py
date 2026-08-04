import json

from app.gateway.schemas import (
    WebUILayoutSettingsDTO,
    WebUISessionSidebarSettingsDTO,
    WebUISettingsUpdateDTO,
)
from app.gateway.ui_settings import merge_web_ui_settings, read_web_ui_settings


def test_gateway_ui_settings_persist_workbench_view(tmp_path):
    updated = merge_web_ui_settings(
        WebUISettingsUpdateDTO(
            layout=WebUILayoutSettingsDTO(workbench_view="gateway")
        ),
        gateway_root=tmp_path,
    )

    assert updated.layout.workbench_view == "gateway"
    assert read_web_ui_settings(tmp_path).layout.workbench_view == "gateway"


def test_gateway_ui_settings_persist_automation_auxiliary_tab(tmp_path):
    updated = merge_web_ui_settings(
        WebUISettingsUpdateDTO(
            layout=WebUILayoutSettingsDTO(auxiliary_tab="automation")
        ),
        gateway_root=tmp_path,
    )

    assert updated.layout.auxiliary_tab == "automation"
    assert read_web_ui_settings(tmp_path).layout.auxiliary_tab == "automation"


def test_gateway_ui_settings_persist_collapsed_workspace_ids(tmp_path):
    merge_web_ui_settings(
        WebUISettingsUpdateDTO(
            session_sidebar=WebUISessionSidebarSettingsDTO(
                filter_mode="attachments"
            )
        ),
        gateway_root=tmp_path,
    )
    updated = merge_web_ui_settings(
        WebUISettingsUpdateDTO(
            session_sidebar=WebUISessionSidebarSettingsDTO(
                collapsed_workspace_ids=["workspace-a", "workspace-b"]
            )
        ),
        gateway_root=tmp_path,
    )

    assert updated.session_sidebar.collapsed_workspace_ids == [
        "workspace-a",
        "workspace-b",
    ]
    assert updated.session_sidebar.filter_mode == "attachments"
    assert read_web_ui_settings(tmp_path).session_sidebar.collapsed_workspace_ids == [
        "workspace-a",
        "workspace-b",
    ]


def test_gateway_ui_settings_migrates_legacy_collapsed_workspace_ids(tmp_path):
    (tmp_path / "web_ui_settings.json").write_text(
        json.dumps(
            {
                "layout": {"collapsed_workspace_ids": ["workspace-b", "workspace-a"]},
                "recent_local_workspace_paths": [],
            }
        ),
        encoding="utf-8",
    )

    settings = read_web_ui_settings(tmp_path)

    assert settings.session_sidebar.collapsed_workspace_ids == [
        "workspace-a",
        "workspace-b",
    ]
