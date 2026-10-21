from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.support import e2e_test_control


@dataclass
class ControlLifetime:
    root: Path
    server: Mock
    thread: Mock
    thread_factory: Mock
    cleanups: list[str]

    def construct(self) -> e2e_test_control.E2ETestControlHarness:
        return e2e_test_control.E2ETestControlHarness(self.root)


@pytest.fixture
def control_lifetime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ControlLifetime:
    server, thread = Mock(), Mock()
    thread.is_alive.return_value = False
    cleanups: list[str] = []
    server.shutdown.side_effect = lambda: cleanups.append("shutdown")
    server.server_close.side_effect = lambda: cleanups.append("socket-close")
    thread.join.side_effect = lambda **_kwargs: cleanups.append("join")
    thread_factory = Mock(return_value=thread)
    monkeypatch.setattr(e2e_test_control, "_ControlServer", Mock(return_value=server))
    monkeypatch.setattr(e2e_test_control.threading, "Thread", thread_factory)
    root = tmp_path / "test-control"
    original_write = Path.write_text

    def write_events(path: Path, text: str, *args: object, **kwargs: object) -> int:
        if path == root / "events.json":
            cleanups.append("events")
        return original_write(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_events)
    return ControlLifetime(root, server, thread, thread_factory, cleanups)


@pytest.mark.parametrize("stage", ["construct-thread", "create-root", "start-thread"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_control_setup_failure_closes_socket_without_stopping_unstarted_thread(
    control_lifetime: ControlLifetime, monkeypatch: pytest.MonkeyPatch,
    stage: str, interrupted: bool,
) -> None:
    state = control_lifetime
    error = KeyboardInterrupt("控制端口启动中断") if interrupted else OSError("控制端口启动失败")
    if stage == "create-root":
        original_mkdir = Path.mkdir

        def fail_mkdir(path: Path, *args: object, **kwargs: object) -> None:
            if path == state.root:
                raise error
            original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    else:
        action = state.thread_factory if stage == "construct-thread" else state.thread.start
        action.side_effect = error

    with pytest.raises(type(error)) as failure:
        state.construct().start()

    assert failure.value is error
    state.server.server_close.assert_called_once_with()
    state.server.shutdown.assert_not_called()
    state.thread.join.assert_not_called()
    if stage == "start-thread":
        assert json.loads((state.root / "events.json").read_text()) == []


def test_control_close_before_start_only_releases_bound_socket(control_lifetime: ControlLifetime) -> None:
    state = control_lifetime
    state.construct().close()
    assert state.cleanups == ["socket-close"]
    state.server.shutdown.assert_not_called()
    state.thread.join.assert_not_called()
    assert not state.root.exists()


@pytest.mark.parametrize("thread_alive", [False, True])
def test_control_close_releases_barriers_and_saves_events_even_if_thread_remains(
    control_lifetime: ControlLifetime, thread_alive: bool,
) -> None:
    state = control_lifetime
    control = state.construct()
    control.start()
    control.arm("operation", "phase")
    control.arm_match("match", "other-phase", {"session_id": "fixture-session"})
    events = [{"operation_id": "operation", "phase": "phase", "pid": 1, "at": 2.0}]
    control.events.extend(events)
    state.thread.is_alive.return_value = thread_alive

    if thread_alive:
        with pytest.raises(RuntimeError, match="E2ETestControlHarness 未停止"):
            control.close()
    else:
        control.close()

    assert sorted(state.cleanups) == ["events", "join", "shutdown", "socket-close"]
    state.thread.join.assert_called_once_with(timeout=10)
    assert control._stopped
    assert control._barriers["operation", "phase"].released.is_set()
    assert control._phase_matches["match"].barrier.released.is_set()
    assert json.loads((state.root / "events.json").read_text()) == events


@pytest.mark.parametrize("stage", ["shutdown", "join", "socket-close", "events"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_control_cleanup_failure_still_attempts_other_resources_and_keeps_original_error(
    control_lifetime: ControlLifetime, monkeypatch: pytest.MonkeyPatch,
    stage: str, interrupted: bool,
) -> None:
    state = control_lifetime
    control = state.construct()
    control.start()
    error = KeyboardInterrupt("控制端口清理中断") if interrupted else OSError("控制端口清理失败")
    if stage == "events":
        original_write = Path.write_text

        def fail_write(path: Path, text: str, *args: object, **kwargs: object) -> int:
            if path == state.root / "events.json":
                state.cleanups.append("events")
                raise error
            return original_write(path, text, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail_write)
    else:
        action = {"shutdown": state.server.shutdown, "join": state.thread.join, "socket-close": state.server.server_close}[stage]

        def fail_cleanup(**_kwargs: object) -> None:
            state.cleanups.append(stage)
            raise error

        action.side_effect = fail_cleanup

    with pytest.raises(type(error)) as failure:
        control.close()

    assert failure.value is error
    assert sorted(state.cleanups) == ["events", "join", "shutdown", "socket-close"]
    if stage != "events":
        assert json.loads((state.root / "events.json").read_text()) == []
