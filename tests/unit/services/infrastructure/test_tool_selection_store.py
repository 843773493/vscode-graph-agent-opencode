from pathlib import Path

from app.services.infrastructure.tool_selection_store import ToolSelectionStore


def test_tool_selection_persists_disabled_names_and_new_tools_default_enabled(
    tmp_path: Path,
) -> None:
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")
    disabled = store.apply_changes(
        agent_id="default",
        changes={"apply_patch": False},
    )

    assert disabled == {"apply_patch"}
    assert store.disabled_tools("default") == {"apply_patch"}
    assert "future_tool" not in store.disabled_tools("default")

    disabled = store.apply_changes(
        agent_id="default",
        changes={"read_file": False, "apply_patch": True},
    )

    assert disabled == {"read_file"}
    assert store.disabled_tools("default") == {"read_file"}


def test_tool_selection_keeps_agent_settings_independent(tmp_path: Path) -> None:
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    store.apply_changes(agent_id="default", changes={"read_file": False})
    store.apply_changes(agent_id="reviewer", changes={"apply_patch": False})

    assert store.disabled_tools("default") == {"read_file"}
    assert store.disabled_tools("reviewer") == {"apply_patch"}
