import pytest
from pydantic import ValidationError

from app.schemas.gateway.ui_settings import (
    WebUILayoutSettingsDTO,
    WebUISettingsDTO,
    WebUISettingsUpdateDTO,
    WebUIWorkspaceBottomPanelSettingsDTO,
)


def test_gateway_ui_settings_rejects_removed_automation_auxiliary_tab():
    with pytest.raises(ValidationError, match="auxiliary_tab"):
        WebUILayoutSettingsDTO.model_validate({"auxiliary_tab": "automation"})


def test_gateway_ui_settings_rejects_removed_gateway_bottom_panel_tab():
    with pytest.raises(ValidationError, match="tab"):
        WebUIWorkspaceBottomPanelSettingsDTO.model_validate({"tab": "gateway"})








def test_gateway_ui_settings_rejects_incomplete_auxiliary_tab_order():
    with pytest.raises(ValidationError, match="不得重复"):
        WebUILayoutSettingsDTO.model_validate(
            {"auxiliary_tab_order": ["files", "changes", "debug", "debug"]}
        )




def test_gateway_ui_settings_normalizes_existing_order_without_reordering_tabs():
    settings = WebUILayoutSettingsDTO(
        auxiliary_tab_order=["resources", "debug", "changes", "files"],
    )
    assert settings.model_dump()["auxiliary_tab_order"] == [
        "resources", "goal", "debug", "changes", "files",
    ]


@pytest.mark.parametrize("order", [
    ["files", "changes", "goal", "debug", "debug"],
    ["files", "changes", "goal", "resources"],
])
def test_gateway_ui_settings_rejects_missing_or_repeated_tabs_with_goal(order):
    with pytest.raises(ValidationError, match="全部标签"):
        WebUILayoutSettingsDTO(auxiliary_tab_order=order)




def test_gateway_ui_settings_rejects_removed_layout_fields():
    with pytest.raises(ValidationError, match="collapsed_workspace_ids"):
        WebUISettingsDTO.model_validate(
            {
                "layout": {"collapsed_workspace_ids": ["workspace-b", "workspace-a"]},
            }
        )


@pytest.mark.parametrize("model", [WebUISettingsDTO, WebUISettingsUpdateDTO])
def test_ui_settings_rejects_native_directory_history(model):
    with pytest.raises(ValidationError, match="recent_local_workspace_paths"):
        model.model_validate({"recent_local_workspace_paths": ["/private/native-directory"]})


@pytest.fixture
def file_preference_payload():
    def payload(field, value):
        if field == "expanded_paths_by_workspace":
            return {"workspace_file_tree": {field: {"target": [value]}}}
        return {"layout": {field: value if field.endswith("active_file_path") else [value]}}
    return payload


@pytest.mark.parametrize("model", [WebUISettingsDTO, WebUISettingsUpdateDTO])
@pytest.mark.parametrize("field", ["workspace_preview_file_paths", "workspace_preview_active_file_path", "expanded_paths_by_workspace"])
@pytest.mark.parametrize("value", [
    "/private/native/file.txt", r"C:\private\file.txt", r"\\server\share\file.txt",
    "src/file.txt", "", "file:///private/file.txt",
    "boxteam://workspace/target/resources/files/root/../file.txt",
    "boxteam://workspace/target/resources/files/root/a%2Fb.txt",
    "boxteam://workspace/target/resources/config/workspace",
])
def test_ui_file_preferences_reject_paths_and_nonfile_uris(model, field, value, file_preference_payload):
    with pytest.raises(ValidationError):
        model.model_validate(file_preference_payload(field, value))


@pytest.mark.parametrize("root", [
    "boxteam://workspace/absent-target/resources/files/root",
    "boxteam://workspace/absent-target/resources/attachments/att_0123456789abcdef0123456789abcdef",
])
def test_ui_file_preferences_preserve_target_dangling_uri_and_canonical_encoding(root):
    uri = root + "/%e4%b8%ad%20%252F.txt"
    canonical = root + "/%E4%B8%AD%20%252F.txt"
    settings = WebUISettingsDTO(
        layout={"workspace_preview_file_paths": [uri], "workspace_preview_active_file_path": uri},
        workspace_file_tree={"expanded_paths_by_workspace": {"routing-target": [root, root]}})
    assert settings.layout.workspace_preview_file_paths == [canonical]
    assert settings.layout.workspace_preview_active_file_path == canonical
    assert settings.workspace_file_tree.expanded_paths_by_workspace == {"routing-target": [root]}
