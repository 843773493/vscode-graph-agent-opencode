from pathlib import Path

from app.services.infrastructure.tool_selection_store import ToolSelectionStore


def test_tool_selection_persists_execution_and_model_visibility(
    tmp_path: Path,
) -> None:
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")
    store.apply_changes(
        agent_id="default",
        changes={"apply_patch": (False, False)},
    )

    assert store.disabled_tools("default") == {"apply_patch"}
    assert store.model_hidden_tools(
        "default", default_hidden_tool_names={"extension_tool"}
    ) == {"apply_patch", "extension_tool"}
    assert "future_tool" not in store.disabled_tools("default")

    store.apply_changes(
        agent_id="default",
        changes={
            "read_file": (False, False),
            "apply_patch": (True, True),
        },
    )

    assert store.disabled_tools("default") == {"read_file"}
    assert store.model_hidden_tools(
        "default", default_hidden_tool_names={"extension_tool"}
    ) == {"read_file", "extension_tool"}


def test_source_debugging_is_hidden_by_default_but_can_be_enabled(
    tmp_path: Path,
) -> None:
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    assert store.model_hidden_tools(
        "default", default_hidden_tool_names={"start_debugging"}
    ) == {"start_debugging"}

    store.apply_changes(
        agent_id="default",
        changes={"start_debugging": (True, True)},
    )

    assert store.model_hidden_tools(
        "default", default_hidden_tool_names={"start_debugging"}
    ) == set()


def test_tool_selection_keeps_agent_settings_independent(tmp_path: Path) -> None:
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    store.apply_changes(
        agent_id="default",
        changes={"read_file": (False, False)},
    )
    store.apply_changes(
        agent_id="reviewer",
        changes={"apply_patch": (False, False)},
    )

    assert store.disabled_tools("default") == {"read_file"}
    assert store.disabled_tools("reviewer") == {"apply_patch"}
