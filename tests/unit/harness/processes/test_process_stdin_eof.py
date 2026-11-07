from __future__ import annotations

import subprocess
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from tests.support.processes import workspace


@pytest.fixture
def owned_process(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    process = Mock(spec=subprocess.Popen, pid=4312, returncode=0)
    process.stdin = StringIO()
    process.poll.return_value = None
    server = Mock(spec=psutil.Process, pid=4313)
    descendant = Mock(spec=psutil.Process, pid=4314)
    owner = Mock(spec=psutil.Process)
    owner.children.side_effect = lambda **kwargs: [server, descendant] if kwargs.get("recursive") else [server]
    wait = Mock(return_value=([server, descendant], []))
    factory = Mock(return_value=owner)
    monkeypatch.setattr(workspace, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(workspace, "psutil", SimpleNamespace(
        Process=factory, NoSuchProcess=psutil.NoSuchProcess, wait_procs=wait,
    ))
    return SimpleNamespace(process=process, children=[server, descendant], factory=factory, wait=wait)


def test_eof_waits_for_owned_descendants_with_one_deadline(
    owned_process: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = owned_process
    clock = iter([10, 11, 13])
    monkeypatch.setattr(workspace.time, "monotonic", lambda: next(clock))
    workspace.terminate_process(tree.process, stdin_eof=True, timeout_seconds=10)
    assert tree.process.stdin.closed
    tree.process.terminate.assert_not_called()
    tree.process.kill.assert_not_called()
    tree.process.wait.assert_called_once_with(timeout=9)
    tree.wait.assert_called_once_with(tree.children, timeout=7)


def test_eof_timeout_keeps_live_process_and_original_failure(owned_process: SimpleNamespace) -> None:
    tree = owned_process
    error = subprocess.TimeoutExpired("owned-server", 0.1)
    tree.process.wait.side_effect = error
    with pytest.raises(TimeoutError, match="stdin EOF 正常停机尚未完成") as failure:
        workspace.terminate_process(tree.process, stdin_eof=True, timeout_seconds=0.1)
    assert failure.value.__cause__ is error
    assert tree.process.stdin.closed
    tree.process.terminate.assert_not_called()
    tree.process.kill.assert_not_called()
    tree.wait.assert_not_called()


def test_eof_reports_live_descendants_after_launcher_exit(owned_process: SimpleNamespace) -> None:
    tree = owned_process
    tree.wait.return_value = (tree.children[:1], tree.children[1:])
    with pytest.raises(RuntimeError, match=r"child_pids=\[4314\]"):
        workspace.terminate_process(tree.process, stdin_eof=True)
    tree.children[1].kill.assert_not_called()


def test_eof_reports_nonzero_server_exit(owned_process: SimpleNamespace) -> None:
    tree = owned_process
    tree.process.returncode = 3
    with pytest.raises(RuntimeError, match="stdin EOF 停机失败: pid=4312, returncode=3"):
        workspace.terminate_process(tree.process, stdin_eof=True)
    tree.wait.assert_called_once()


def test_eof_requires_explicit_owned_pipe(owned_process: SimpleNamespace) -> None:
    tree = owned_process
    tree.process.stdin = None
    with pytest.raises(ValueError, match="持有 stdin 管道"):
        workspace.terminate_process(tree.process, stdin_eof=True)
    tree.factory.assert_not_called()
    tree.process.wait.assert_not_called()


def test_eof_releases_pipe_without_enumerating_already_exited_pid(owned_process: SimpleNamespace) -> None:
    tree = owned_process
    tree.process.poll.return_value = 0
    workspace.terminate_process(tree.process, stdin_eof=True)
    assert tree.process.stdin.closed
    tree.factory.assert_not_called()


def test_force_still_kills_owned_tree_and_releases_pipe(owned_process: SimpleNamespace) -> None:
    tree = owned_process
    workspace.terminate_process(tree.process, stdin_eof=True, force=True)
    assert tree.process.stdin.closed
    tree.process.kill.assert_called_once_with()
    for child in tree.children:
        child.kill.assert_called_once_with()
    tree.wait.assert_called_once_with(tree.children, timeout=10)


def test_eof_already_failed_process_is_not_reported_as_success(owned_process: SimpleNamespace) -> None:
    tree = owned_process
    tree.process.poll.return_value = tree.process.returncode = 3
    with pytest.raises(RuntimeError, match="returncode=3"):
        workspace.terminate_process(tree.process, stdin_eof=True)
    assert tree.process.stdin.closed
    tree.factory.assert_not_called()


@pytest.mark.parametrize("force", [False, True])
def test_eof_retry_keeps_owned_snapshot_after_launcher_exit(
    owned_process: SimpleNamespace, force: bool,
) -> None:
    tree = owned_process
    captured: list[psutil.Process] = []
    observed: list[tuple[psutil.Process, ...]] = []

    def wait(children: list[psutil.Process], **_kwargs: object):
        observed.append(tuple(children))
        return ([], tree.children) if len(observed) == 1 else (tree.children, [])

    tree.wait.side_effect = wait
    with pytest.raises(RuntimeError, match="child_pids"):
        workspace.terminate_process(tree.process, stdin_eof=True, owned_children=captured)
    assert captured == tree.children
    tree.process.poll.return_value = 0
    tree.factory.reset_mock()
    workspace.terminate_process(tree.process, stdin_eof=True, owned_children=captured, force=force)
    assert observed == [tuple(tree.children), tuple(tree.children)]
    assert captured == []
    tree.factory.assert_not_called()
    for child in tree.children:
        assert child.kill.call_count == int(force)
