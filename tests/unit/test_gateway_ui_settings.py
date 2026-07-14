from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.gateway.schemas import WebUISettingsUpdateDTO
from app.gateway.ui_settings import merge_web_ui_settings, read_web_ui_settings


@pytest.fixture
def gateway_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("BOXTEAM_WEB_UI_SETTINGS_PATH", raising=False)
    return tmp_path / "gateway"


def test_preview_and_agent_sessions_layout_persist_across_reads(
    gateway_root: Path,
) -> None:
    updated = merge_web_ui_settings(
        WebUISettingsUpdateDTO.model_validate(
            {
                "layout": {
                    "agent_sessions_panel_open": False,
                    "main_area_ratios": {
                        "agent_sessions": 1,
                        "chat": 2,
                        "workspace_preview": 3,
                        "auxiliary": 1,
                    },
                    "workspace_preview_visible": True,
                    "workspace_preview_maximized": True,
                    "workspace_preview_file_paths": ["AGENTS.md", "src/main.py"],
                    "workspace_preview_active_file_path": "AGENTS.md",
                    "auxiliary_visible": True,
                }
            }
        ),
        gateway_root=gateway_root,
    )

    restored = read_web_ui_settings(gateway_root)

    assert restored == updated
    assert restored.layout.agent_sessions_panel_open is False
    assert restored.layout.main_area_ratios is not None
    assert restored.layout.main_area_ratios.model_dump() == {
        "agent_sessions": 1,
        "chat": 2,
        "workspace_preview": 3,
        "auxiliary": 1,
    }
    assert restored.layout.workspace_preview_visible is True
    assert restored.layout.workspace_preview_maximized is True
    assert restored.layout.workspace_preview_file_paths == [
        "AGENTS.md",
        "src/main.py",
    ]
    assert restored.layout.workspace_preview_active_file_path == "AGENTS.md"
    assert restored.layout.auxiliary_visible is True

    stored = json.loads(
        (gateway_root / "web_ui_settings.json").read_text(encoding="utf-8")
    )
    assert stored["layout"]["workspace_preview_maximized"] is True


def test_partial_layout_update_preserves_preview_state(gateway_root: Path) -> None:
    merge_web_ui_settings(
        WebUISettingsUpdateDTO.model_validate(
            {
                "layout": {
                    "workspace_preview_visible": True,
                    "main_area_ratios": {
                        "agent_sessions": 1,
                        "chat": 1,
                        "workspace_preview": 1,
                        "auxiliary": 1,
                    },
                    "workspace_preview_maximized": True,
                    "workspace_preview_file_paths": ["AGENTS.md"],
                    "workspace_preview_active_file_path": "AGENTS.md",
                }
            }
        ),
        gateway_root=gateway_root,
    )

    merge_web_ui_settings(
        WebUISettingsUpdateDTO.model_validate(
            {"layout": {"auxiliary_visible": False}}
        ),
        gateway_root=gateway_root,
    )

    restored = read_web_ui_settings(gateway_root)
    assert restored.layout.auxiliary_visible is False
    assert restored.layout.workspace_preview_visible is True
    assert restored.layout.main_area_ratios is not None
    assert restored.layout.main_area_ratios.model_dump() == {
        "agent_sessions": 1,
        "chat": 1,
        "workspace_preview": 1,
        "auxiliary": 1,
    }
    assert restored.layout.workspace_preview_maximized is True
    assert restored.layout.workspace_preview_file_paths == ["AGENTS.md"]
    assert restored.layout.workspace_preview_active_file_path == "AGENTS.md"
