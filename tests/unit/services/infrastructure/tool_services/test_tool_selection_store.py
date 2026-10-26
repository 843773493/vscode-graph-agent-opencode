from pathlib import Path

import portalocker
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


def test_tool_selection_lock_times_out_when_file_lock_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def reject_nonblocking_lock(_lock_file, flags: int) -> None:
        nonlocal attempts
        attempts += 1
        assert flags & portalocker.LOCK_NB
        raise portalocker.AlreadyLocked("test lock is held")

    monkeypatch.setattr(
        tool_selection_store.portalocker,
        "lock",
        reject_nonblocking_lock,
    )
    monkeypatch.setattr(
        tool_selection_store,
        "_TOOL_SELECTION_LOCK_TIMEOUT_SECONDS",
        0.01,
    )
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    with pytest.raises(TimeoutError, match="工具选择配置锁获取超时"):
        store.snapshot("default")
    assert attempts > 1


def test_tool_selection_uses_shared_reads_and_exclusive_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modes = []
    original_lock = portalocker.lock

    def record_lock(lock_file, flags: int) -> None:
        modes.append(flags)
        original_lock(lock_file, flags)

    monkeypatch.setattr(tool_selection_store.portalocker, "lock", record_lock)
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    store.snapshot("default")
    store.apply_changes(agent_id="default", changes={"read_file": (True, True)})

    assert modes == [
        portalocker.LOCK_SH | portalocker.LOCK_NB,
        portalocker.LOCK_EX | portalocker.LOCK_NB,
    ]


def test_tool_selection_lock_exposes_non_contention_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = portalocker.LockException("invalid lock handle")

    def fail_lock(_lock_file, _flags: int) -> None:
        raise error

    monkeypatch.setattr(tool_selection_store.portalocker, "lock", fail_lock)
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")

    with pytest.raises(portalocker.LockException) as captured:
        store.snapshot("default")

    assert captured.value.__cause__ is error
    assert "error=LockException" in str(captured.value)
    assert str(tmp_path) not in str(captured.value)


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


def test_selection_subscribers_observe_committed_state_and_release_by_agent(tmp_path):
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")
    seen = []
    other = []
    release = store.subscribe("default", lambda: seen.append(store.snapshot("default")))
    release_other = store.subscribe("other", lambda: other.append(store.snapshot("other")))
    assert len(seen) == len(other) == 1
    store.apply_changes(agent_id="default", changes={"echo": (True, True)})
    assert seen[-1].model_visibility_overrides == {"echo": True}
    assert len(seen) == 2 and len(other) == 1
    release()
    release_other()
    store.apply_changes(agent_id="default", changes={"echo": (True, False)})
    assert len(seen) == 2


def test_selection_callback_failure_does_not_hide_commit_or_skip_peer(tmp_path):
    store = ToolSelectionStore(boxteam_root=tmp_path / ".boxteam")
    observed = []
    fail = False

    def broken():
        if fail:
            raise RuntimeError("controlled publication failure")

    release = store.subscribe("default", broken)
    release_peer = store.subscribe("default", lambda: observed.append(store.snapshot("default")))
    fail = True
    with pytest.raises(ExceptionGroup, match="已保存.*发布失败") as caught:
        store.apply_changes(agent_id="default", changes={"echo": (True, True)})
    assert len(caught.value.exceptions) == 1
    assert str(caught.value.exceptions[0]) == "controlled publication failure"
    assert observed[-1] == store.snapshot("default")
    assert observed[-1].model_visibility_overrides == {"echo": True}
    release()
    release_peer()
