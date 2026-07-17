from app.gateway.schemas import WebUILayoutSettingsDTO, WebUISettingsUpdateDTO
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
