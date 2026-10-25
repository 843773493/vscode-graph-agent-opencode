import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import UserAccessService
from app.gateway.control.user_profile import UserProfileStore
from app.gateway.main import app
from app.gateway.ui_settings import merge_web_ui_settings_values
from app.schemas.gateway.ui_settings import (
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
    with pytest.raises(ValidationError, match="不得重复"):
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
        "goal",
        "debug",
        "resources",
    ]


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
    settings = merge_web_ui_settings_values(WebUISettingsDTO(), WebUISettingsUpdateDTO(
        layout={"workspace_preview_file_paths": [uri], "workspace_preview_active_file_path": uri},
        workspace_file_tree={"expanded_paths_by_workspace": {"routing-target": [root, root]}}))
    assert settings.layout.workspace_preview_file_paths == [canonical]
    assert settings.layout.workspace_preview_active_file_path == canonical
    assert settings.workspace_file_tree.expanded_paths_by_workspace == {"routing-target": [root]}


@pytest.fixture
def ui_settings_client(tmp_path, monkeypatch):
    root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(root))
    state = GatewayStateStore(path=root / "gateway.sqlite")
    monkeypatch.setattr(app.state, "user_access_service", UserAccessService(state=state), raising=False)
    monkeypatch.setattr(app.state, "user_profile_store", UserProfileStore(gateway_root=root), raising=False)
    client = TestClient(app)
    try:
        credential = client.get("/api/gateway/auth/local-credential")
        client.headers["X-Local-Token"] = credential.json()["data"]["token"]
        yield client
    finally:
        client.close()
        state.close()


def test_goal_tab_order_http_round_trip(ui_settings_client):
    created = ui_settings_client.post("/api/gateway/users", json={"display_name": "Goal 测试用户"})
    assert created.status_code == 200, created.text
    acquired = ui_settings_client.post(
        f"/api/gateway/users/{created.json()['data']['user_id']}/access",
        json={"client_label": "Goal 布局回归"},
    )
    assert acquired.status_code == 200, acquired.text
    layout = {
        "auxiliary_tab": "goal",
        "auxiliary_tab_order": ["resources", "goal", "files", "changes", "debug"],
    }
    response = ui_settings_client.put("/api/gateway/ui-settings", json={"layout": layout})
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    restored = ui_settings_client.get("/api/gateway/ui-settings")
    assert restored.status_code == 200, restored.text
    for key, value in layout.items():
        assert response.json()["data"]["layout"][key] == value
        assert restored.json()["data"]["layout"][key] == value


@pytest.mark.parametrize("field", ["workspace_preview_file_paths", "workspace_preview_active_file_path", "expanded_paths_by_workspace"])
@pytest.mark.parametrize("value", ["/private/native.txt", r"C:\private\native.txt", "boxteam://workspace/target/resources/files/root/a%2Fb.txt"])
def test_ui_file_preference_http_rejection_does_not_echo_input(ui_settings_client, file_preference_payload, field, value):
    response = ui_settings_client.put("/api/gateway/ui-settings", json=file_preference_payload(field, value))
    assert response.status_code == 422, response.text
    assert value not in response.text
    body = response.json()
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["detail"] == [{"loc": ["body", "workspace_file_tree" if field == "expanded_paths_by_workspace" else "layout", field],
                               "type": "value_error", "msg": "请求参数校验失败"}]
