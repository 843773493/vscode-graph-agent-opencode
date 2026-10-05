import os
from pathlib import Path

import pytest

from app.services.infrastructure import tool_selection_store
from app.services.infrastructure.tool_selection_store import ToolSelectionStore


def test_tool_selection_persists_explicit_runtime_overrides(tmp_path: Path) -> None:
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")
    store.apply_changes(
        agent_id="default",
        changes={"apply_patch": (False, False), "source_debug": (True, True)},
    )

    assert store.snapshot("default").execution_overrides == {
        "apply_patch": False,
        "source_debug": True,
    }
    assert store.snapshot("default").model_visibility_overrides == {
        "apply_patch": False,
        "source_debug": True,
    }


def test_runtime_override_can_enable_a_config_hidden_tool(tmp_path: Path) -> None:
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    store.apply_changes(
        agent_id="default",
        changes={"start_debugging": (True, True)},
    )

    assert store.snapshot("default").execution_overrides["start_debugging"] is True
    assert store.snapshot("default").model_visibility_overrides["start_debugging"] is True


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

    assert store.snapshot("default").execution_overrides == {"read_file": False}
    assert store.snapshot("reviewer").execution_overrides == {"apply_patch": False}


def test_execution_disabled_tool_cannot_be_model_visible(tmp_path: Path) -> None:
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    with pytest.raises(ValueError, match="不能对模型可见"):
        store.apply_changes(
            agent_id="default",
            changes={"read_file": (False, True)},
        )


def test_tool_selection_rejects_retired_disabled_tool_lists(tmp_path: Path) -> None:
    settings_dir = tmp_path / ".boxteam" / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "tool_selection.json").write_text(
        '{"default": ["apply_patch"]}',
        encoding="utf-8",
    )

    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    with pytest.raises(TypeError, match="Agent 项格式错误"):
        store.snapshot("default")


@pytest.mark.skipif(os.name == "nt", reason="仅在 POSIX 上覆盖 fcntl 文件锁")
def test_tool_selection_lock_times_out_when_file_lock_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl

    original_flock = fcntl.flock

    def reject_nonblocking_lock(fd: int, operation: int) -> None:
        if operation & fcntl.LOCK_NB:
            raise BlockingIOError("test lock is held")
        original_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", reject_nonblocking_lock)
    monkeypatch.setattr(
        tool_selection_store,
        "_TOOL_SELECTION_LOCK_TIMEOUT_SECONDS",
        0.01,
    )
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    with pytest.raises(TimeoutError, match="工具选择配置锁获取超时"):
        store.snapshot("default")


def test_snapshot_is_frozen_across_later_selection_changes(tmp_path: Path) -> None:
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")
    store.apply_changes(agent_id="default", changes={"read_file": (True, True)})
    first = store.snapshot("default")
    first.execution_overrides["read_file"] = False
    store.apply_changes(agent_id="default", changes={"read_file": (True, False)})
    second = store.snapshot("default")

    assert first.execution_overrides == {"read_file": True}
    assert first.model_visibility_overrides == {"read_file": True}
    assert second.execution_overrides == {"read_file": True}
    assert second.model_visibility_overrides == {"read_file": False}
    assert first.revision != second.revision
    store.apply_changes(agent_id="default", changes={"read_file": (True, False)})
    assert store.snapshot("default").revision == second.revision
