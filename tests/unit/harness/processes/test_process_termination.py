from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from tests.support.processes import workspace


@pytest.fixture
def process_tree(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    process = Mock(spec=subprocess.Popen, pid=4312)
    process.poll.return_value = None
    child = Mock(spec=psutil.Process, pid=4313)
    owner = Mock(spec=psutil.Process)
    owner.children.return_value = [child]
    events: list[str] = []
    owner.children.side_effect = lambda **_kwargs: events.append("capture") or [child]
    process.terminate.side_effect = lambda: events.append("terminate")
    process.kill.side_effect = lambda: events.append("kill")
    process.wait.side_effect = lambda **_kwargs: events.append("launcher-exited")
    child.kill.side_effect = lambda: events.append("child-kill")
    wait = Mock(side_effect=lambda *_args, **_kwargs: events.append("children-exited") or ([child], []))
    process_factory = Mock(return_value=owner)
    monkeypatch.setattr(workspace, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(workspace, "psutil", SimpleNamespace(
        Process=process_factory, NoSuchProcess=psutil.NoSuchProcess, wait_procs=wait,
    ))
    return SimpleNamespace(process=process, child=child, owner=owner, factory=process_factory, wait=wait, events=events)


@pytest.mark.parametrize("force", [False, True])
def test_windows_stop_waits_for_captured_children_after_launcher_exit(process_tree, force: bool) -> None:
    tree = process_tree
    workspace.terminate_process(tree.process, force=force)

    assert tree.events == [
        "capture", *(["child-kill", "kill"] if force else ["terminate"]),
        "launcher-exited", "children-exited",
    ]
    tree.owner.children.assert_called_once_with(recursive=True)
    tree.wait.assert_called_once_with([tree.child], timeout=10)


def test_windows_stop_reports_live_server_instead_of_claiming_completion(process_tree) -> None:
    tree = process_tree
    tree.wait.side_effect = None
    tree.wait.return_value = ([], [tree.child])
    with pytest.raises(RuntimeError, match=r"launcher_pid=4312, child_pids=\[4313\]"):
        workspace.terminate_process(tree.process)
    tree.child.kill.assert_not_called()


def test_windows_stop_still_waits_children_if_launcher_disappears_during_termination(process_tree) -> None:
    tree = process_tree
    tree.process.terminate.side_effect = ProcessLookupError()
    workspace.terminate_process(tree.process)
    tree.wait.assert_called_once_with([tree.child], timeout=10)


def test_exited_process_does_not_enumerate_unowned_reused_pid(process_tree) -> None:
    tree = process_tree
    tree.process.poll.return_value = 0
    workspace.terminate_process(tree.process)
    tree.factory.assert_not_called()
    tree.wait.assert_not_called()


def test_posix_termination_keeps_single_process_and_kill_escalation(process_tree, monkeypatch: pytest.MonkeyPatch) -> None:
    tree = process_tree
    monkeypatch.setattr(workspace, "sys", SimpleNamespace(platform="linux"))
    tree.process.wait.side_effect = [subprocess.TimeoutExpired("server", 10), 0]
    workspace.terminate_process(tree.process)
    tree.process.terminate.assert_called_once_with()
    tree.process.kill.assert_called_once_with()
    assert tree.process.wait.call_count == 2
    tree.factory.assert_not_called()
    tree.wait.assert_not_called()
