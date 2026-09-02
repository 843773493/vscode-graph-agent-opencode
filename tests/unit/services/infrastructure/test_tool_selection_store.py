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

    assert store.execution_overrides("default") == {
        "apply_patch": False,
        "source_debug": True,
    }
    assert store.model_visibility_overrides("default") == {
        "apply_patch": False,
        "source_debug": True,
    }
    assert store.disabled_tools("default") == {"apply_patch"}


def test_runtime_override_can_enable_a_config_hidden_tool(tmp_path: Path) -> None:
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    store.apply_changes(
        agent_id="default",
        changes={"start_debugging": (True, True)},
    )

    assert store.execution_overrides("default")["start_debugging"] is True
    assert store.model_visibility_overrides("default")["start_debugging"] is True


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


def test_execution_disabled_tool_cannot_be_model_visible(tmp_path: Path) -> None:
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    with pytest.raises(ValueError, match="不能对模型可见"):
        store.apply_changes(
            agent_id="default",
            changes={"read_file": (False, True)},
        )


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
        store.execution_overrides("default")
