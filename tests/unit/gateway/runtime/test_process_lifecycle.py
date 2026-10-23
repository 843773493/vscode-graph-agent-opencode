import os
import subprocess
from types import SimpleNamespace

import psutil
import pytest

from app.gateway.runtime.process_lifecycle import (
    AdoptedManagedProcess,
    ManagedProcess,
)


class _LogFile:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组信号仅在 POSIX 上可用")
def test_managed_process_closes_posix_process_group(
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
) -> None:
    process = managed_process_stub_factory(timeout_once=False)
    log_file = _LogFile()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle._posix_group_is_alive",
        lambda _pid, _starttime: process.poll() is None,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle.os.killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    ManagedProcess(process=process, log_file=log_file).close()

    assert signals == [(4321, 15)]
    assert log_file.closed is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX 进程组信号仅在 POSIX 上可用")
def test_managed_process_kills_group_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
) -> None:
    process = managed_process_stub_factory(timeout_once=True)
    log_file = _LogFile()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle._posix_group_is_alive",
        lambda _pid, _starttime: process.poll() is None,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle.os.killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    managed = ManagedProcess(process=process, log_file=log_file)
    with pytest.raises(TimeoutError, match="未完成优雅关闭") as raised:
        managed.close(timeout_seconds=0.01)

    assert signals == [(4321, 15), (4321, 9)]
    assert process.wait_calls == 2
    assert managed.close_failure is raised.value
    assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)
    assert log_file.closed is False
    with pytest.raises(TimeoutError) as retried:
        managed.close(timeout_seconds=0.01)
    assert retried.value is raised.value
    assert process.wait_calls == 2


def test_managed_process_windows_stops_complete_process_tree(
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
    windows_process_tree_stub,
) -> None:
    process = managed_process_stub_factory(timeout_once=False)
    log_file = _LogFile()
    calls: list[list[str]] = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle.subprocess.run",
        lambda arguments, **_: (
            calls.append(arguments) or SimpleNamespace(returncode=0, stderr="")
        ),
    )

    ManagedProcess(process=process, log_file=log_file).close()

    assert calls == [["taskkill", "/T", "/PID", "4321"]]
    assert log_file.closed is True


def test_managed_process_windows_force_kills_complete_process_tree_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
    windows_process_tree_stub,
) -> None:
    process = managed_process_stub_factory(timeout_once=True)
    log_file = _LogFile()
    calls: list[list[str]] = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle.subprocess.run",
        lambda arguments, **_: (
            calls.append(arguments) or SimpleNamespace(returncode=0, stderr="")
        ),
    )

    managed = ManagedProcess(process=process, log_file=log_file)
    with pytest.raises(TimeoutError, match="未完成优雅关闭") as raised:
        managed.close(timeout_seconds=0.01)

    assert calls == [
        ["taskkill", "/T", "/PID", "4321"],
        ["taskkill", "/T", "/F", "/PID", "4321"],
    ]
    assert process.wait_calls == 2
    assert managed.close_failure is raised.value
    assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)
    assert log_file.closed is False
    with pytest.raises(TimeoutError) as retried:
        managed.close(timeout_seconds=0.01)
    assert retried.value is raised.value
    assert process.wait_calls == 2


def test_managed_process_windows_waits_for_descendants_after_root_exits(
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
    windows_process_tree_stub,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    root = windows_process_tree_stub.add_process(
        4321,
        parent_pid=None,
        starttime=1.0,
    )
    child = windows_process_tree_stub.add_process(
        5001,
        parent_pid=4321,
        starttime=2.0,
        exit_on_terminate=False,
    )
    process = managed_process_stub_factory(timeout_once=False)
    process_wait = process.wait

    def wait_for_root(timeout: float) -> int:
        root.alive = False
        return process_wait(timeout)

    monkeypatch.setattr(process, "wait", wait_for_root)
    log_file = _LogFile()
    taskkill_calls: list[list[str]] = []
    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle.subprocess.run",
        lambda arguments, **_: (
            taskkill_calls.append(arguments) or SimpleNamespace(returncode=0, stderr="")
        ),
    )

    managed = ManagedProcess(process=process, log_file=log_file)
    with pytest.raises(TimeoutError, match="未完成优雅关闭"):
        managed.close(timeout_seconds=0.01)

    assert root.alive is False
    assert child.kill_calls == 1
    assert taskkill_calls == [["taskkill", "/T", "/PID", "4321"]]
    assert process.wait_calls == 2
    assert log_file.closed is False


def test_managed_process_windows_stops_descendants_if_root_exited_early(
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
    windows_process_tree_stub,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    child = windows_process_tree_stub.add_process(
        5001,
        parent_pid=4321,
        starttime=2.0,
    )
    process = managed_process_stub_factory(timeout_once=False)
    process.returncode = 0
    log_file = _LogFile()
    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("根进程已退出后不应再调用 taskkill"),
    )

    ManagedProcess(process=process, log_file=log_file).close()

    assert child.terminate_calls == 1
    assert child.alive is False
    assert process.wait_calls == 1
    assert log_file.closed is True


def test_adopted_process_windows_stops_descendants_if_root_exited_early(
    monkeypatch: pytest.MonkeyPatch,
    windows_process_tree_stub,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(psutil, "pid_exists", lambda _pid: False)
    child = windows_process_tree_stub.add_process(
        5001,
        parent_pid=4321,
        starttime=2.0,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("根进程已退出后不应再调用 taskkill"),
    )

    AdoptedManagedProcess(pid=4321).close()

    assert child.terminate_calls == 1
    assert child.alive is False


def test_adopted_process_windows_checks_liveness_without_spawning_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_pids: list[int] = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        psutil,
        "pid_exists",
        lambda pid: checked_pids.append(pid) or True,
    )
    monkeypatch.setattr(
        "app.gateway.runtime.process_lifecycle.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("进程探活不得启动外部命令"),
    )

    assert AdoptedManagedProcess(pid=4321)._is_alive() is True
    assert checked_pids == [4321]


def test_adopted_process_windows_reports_missing_pid_as_not_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(psutil, "pid_exists", lambda _pid: False)

    assert AdoptedManagedProcess(pid=4321)._is_alive() is False


def test_adopted_process_windows_surfaces_process_query_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_query(_pid: int) -> bool:
        raise psutil.AccessDenied(pid=4321)

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        psutil,
        "pid_exists",
        fail_query,
    )

    with pytest.raises(RuntimeError, match="Windows 进程状态查询失败") as raised:
        AdoptedManagedProcess(pid=4321)._is_alive()

    assert isinstance(raised.value.__cause__, psutil.AccessDenied)
