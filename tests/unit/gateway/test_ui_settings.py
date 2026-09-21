import pytest
from pydantic import ValidationError

from app.gateway.ui_settings import merge_web_ui_settings_values
from app.schemas.gateway import (
    WebUILayoutSettingsDTO,
    WebUISessionSidebarSettingsDTO,
    WebUISettingsDTO,
    WebUISettingsUpdateDTO,
    WebUIWorkspaceBottomPanelSettingsDTO,
)


def test_gateway_ui_settings_merge_workbench_view():
    updated = merge_web_ui_settings_values(
        WebUISettingsDTO(),
        WebUISettingsUpdateDTO(layout=WebUILayoutSettingsDTO(workbench_view="gateway")),
    )

    assert updated.layout.workbench_view == "gateway"


def test_gateway_ui_settings_rejects_removed_automation_auxiliary_tab():
    with pytest.raises(ValidationError, match="auxiliary_tab"):
        WebUILayoutSettingsDTO.model_validate({"auxiliary_tab": "automation"})


def test_gateway_ui_settings_rejects_removed_gateway_bottom_panel_tab():
    with pytest.raises(ValidationError, match="tab"):
        WebUIWorkspaceBottomPanelSettingsDTO.model_validate({"tab": "gateway"})


def test_gateway_ui_settings_merge_workspace_automation_panel_state():
    updated = merge_web_ui_settings_values(
        WebUISettingsDTO(),
        WebUISettingsUpdateDTO(
            layout={
                "bottom_panel_by_workspace": {
                    "workspace-home": {
                        "visible": True,
                        "height": 320,
                        "tab": "automation",
                    }
                }
            }
        ),
    )

    panel = updated.layout.bottom_panel_by_workspace["workspace-home"]
    assert panel.tab == "automation"
    assert panel.height == 320


def test_gateway_ui_settings_merge_workspace_terminal_panel_state():
    updated = merge_web_ui_settings_values(
        WebUISettingsDTO(),
        WebUISettingsUpdateDTO(
            layout={
                "bottom_panel_by_workspace": {
                    "workspace-home": {
                        "visible": True,
                        "height": 320,
                        "tab": "terminal",
                        "terminal_id": "terminal-1",
                    }
                }
            }
        ),
    )

    panel = updated.layout.bottom_panel_by_workspace["workspace-home"]
    assert panel.tab == "terminal"
    assert panel.height == 320
    assert panel.terminal_id == "terminal-1"


def test_gateway_ui_settings_merge_workspace_port_panel_state():
    updated = merge_web_ui_settings_values(
        WebUISettingsDTO(),
        WebUISettingsUpdateDTO(
            layout={
                "bottom_panel_by_workspace": {
                    "workspace-home": {
                        "visible": True,
                        "height": 320,
                        "tab": "ports",
                    }
                }
            }
        ),
    )

    assert updated.layout.bottom_panel_by_workspace["workspace-home"].tab == "ports"


def test_gateway_ui_settings_rejects_incomplete_auxiliary_tab_order():
    with pytest.raises(ValidationError, match="全部四个标签"):
        WebUILayoutSettingsDTO.model_validate(
            {"auxiliary_tab_order": ["files", "changes", "debug", "debug"]}
        )


def test_gateway_ui_settings_merge_debug_auxiliary_tab():
    updated = merge_web_ui_settings_values(
        WebUISettingsDTO(),
        WebUISettingsUpdateDTO(
            layout=WebUILayoutSettingsDTO(
                auxiliary_tab="debug",
                auxiliary_tab_order=["files", "changes", "debug", "resources"],
            )
        ),
    )

    assert updated.layout.auxiliary_tab == "debug"
    assert updated.layout.auxiliary_tab_order == [
        "files",
        "changes",
        "debug",
        "resources",
    ]


def test_gateway_ui_settings_merge_preserves_unpatched_sidebar_values():
    current = WebUISettingsDTO(
        session_sidebar=WebUISessionSidebarSettingsDTO(filter_mode="attachments")
    )
    updated = merge_web_ui_settings_values(
        current,
        WebUISettingsUpdateDTO(
            session_sidebar=WebUISessionSidebarSettingsDTO(
                collapsed_workspace_ids=["workspace-a", "workspace-b"]
            )
        ),
    )

    assert updated.session_sidebar.collapsed_workspace_ids == [
        "workspace-a",
        "workspace-b",
    ]
    assert updated.session_sidebar.filter_mode == "attachments"


def test_gateway_ui_settings_rejects_removed_layout_fields():
    with pytest.raises(ValidationError, match="collapsed_workspace_ids"):
        WebUISettingsDTO.model_validate(
            {
                "layout": {"collapsed_workspace_ids": ["workspace-b", "workspace-a"]},
                "recent_local_workspace_paths": [],
            }
        )
