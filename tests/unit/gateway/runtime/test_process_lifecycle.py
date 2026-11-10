import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from app.gateway.runtime.process_lifecycle import (
    AdoptedManagedProcess,
    ManagedProcess,
    _capture_windows_process_tree,
    wait_process_exit,
)


@pytest.fixture
def exit_notification_process():
    process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE)
    try:
        yield process
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=5)


async def test_exit_notification_waits_for_actual_exit(exit_notification_process) -> None:
    process = exit_notification_process
    waiting = asyncio.create_task(wait_process_exit(process.pid))
    await asyncio.sleep(0)
    assert not waiting.done()
    process.stdin.close()
    process.stdin = None
    await asyncio.wait_for(waiting, 3)
    assert process.wait(timeout=1) == 0


async def test_exit_notification_cancellation_keeps_process_alive(exit_notification_process) -> None:
    process = exit_notification_process
    waiting = asyncio.create_task(wait_process_exit(process.pid))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiting, 3)
    assert process.poll() is None


async def test_exit_notification_accepts_already_exited_process(exit_notification_process) -> None:
    process = exit_notification_process
    process.stdin.close()
    process.stdin = None
    assert process.wait(timeout=3) == 0
    await asyncio.wait_for(wait_process_exit(process.pid), 3)


class _LogFile:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("root_alive", [True, False])
def test_windows_process_tree_uses_one_parent_snapshot(
    windows_process_tree_stub, root_alive: bool,
) -> None:
    tree = windows_process_tree_stub
    root = tree.add_process(4321, parent_pid=5002, starttime=1.0)
    root.alive = root_alive
    tree.add_process(5001, parent_pid=4321, starttime=2.0)
    tree.add_process(5002, parent_pid=5001, starttime=3.0)
    tree.add_process(6001, parent_pid=None, starttime=4.0)
    tree.add_process(6002, parent_pid=6001, starttime=5.0)

    root_start_time, descendants = _capture_windows_process_tree(
        4321, expected_root_start_time=1.0,
    )

    assert root_start_time == (1.0 if root_alive else None)
    assert descendants == {5001: 2.0, 5002: 3.0}
    assert tree.parent_map_reads == 1
    assert tree.process_iter_attrs == [["pid", "create_time"]]


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
    monkeypatch.setattr(os, "name", "nt")

    ManagedProcess(process=process, log_file=log_file).close()

    assert process.terminate_calls == 1
    assert log_file.closed is True


def test_managed_process_windows_force_kills_complete_process_tree_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
    windows_process_tree_stub,
) -> None:
    process = managed_process_stub_factory(timeout_once=True)
    log_file = _LogFile()
    monkeypatch.setattr(os, "name", "nt")

    managed = ManagedProcess(process=process, log_file=log_file)
    with pytest.raises(TimeoutError, match="未完成优雅关闭") as raised:
        managed.close(timeout_seconds=0.01)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
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
    managed = ManagedProcess(process=process, log_file=log_file)
    with pytest.raises(TimeoutError, match="未完成优雅关闭"):
        managed.close(timeout_seconds=0.01)

    assert root.alive is False
    assert child.kill_calls == 1
    assert process.terminate_calls == 1
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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc 进程组成员核实")
def test_adopted_process_linux_treats_zombie_only_group_as_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.gateway.runtime import process_groups

    monkeypatch.setattr(
        process_groups,
        "_read_linux_process_identity",
        lambda pid: process_groups._ProcessIdentity("Z", 4321, 4321, 100)
        if pid == 4321 else None,
    )
    monkeypatch.setattr(process_groups.os, "killpg", lambda *_: None)
    monkeypatch.setattr(
        process_groups.Path,
        "iterdir",
        lambda _path: [process_groups.Path("/proc/4321")],
    )

    assert AdoptedManagedProcess(pid=4321).is_alive() is False


def test_windows_service_identity_is_frozen_and_drives_liveness(
    monkeypatch, managed_process_stub_factory, windows_process_tree_stub,
) -> None:
    tree = windows_process_tree_stub
    tree.add_process(4321, parent_pid=None, starttime=1.0)
    worker = tree.add_process(5001, parent_pid=4321, starttime=2.0)
    monkeypatch.setattr(os, "name", "nt")
    process = managed_process_stub_factory(timeout_once=False)
    managed = ManagedProcess(process, _LogFile())
    managed.confirm_service_identity(5001)
    assert managed.pid == 5001 and managed.process.pid == 4321
    assert managed.is_alive()
    with pytest.raises(RuntimeError, match="已确认.*身份已变化"):
        managed.confirm_service_identity(4321)
    worker.starttime = 3.0
    assert not managed.is_alive()
    assert managed.pid == 5001
    with pytest.raises(RuntimeError, match="已确认.*身份已变化"):
        managed.confirm_service_identity(5001)


def test_windows_service_identity_rejects_pid_reuse_between_snapshots(
    monkeypatch, managed_process_stub_factory, windows_process_tree_stub,
) -> None:
    tree = windows_process_tree_stub
    tree.add_process(4321, parent_pid=None, starttime=1.0)
    tree.add_process(5001, parent_pid=4321, starttime=2.0)
    monkeypatch.setattr(os, "name", "nt")
    managed = ManagedProcess(managed_process_stub_factory(timeout_once=False), _LogFile())

    def replace_worker_after_parent_snapshot() -> dict[int, int]:
        parents = tree.parent_pid_map()
        tree.add_process(5001, parent_pid=None, starttime=9.0)
        return parents

    monkeypatch.setattr(psutil, "_ppid_map", replace_worker_after_parent_snapshot)

    with pytest.raises(RuntimeError, match="服务进程身份在确认期间已变化"):
        managed.confirm_service_identity(5001)

    assert managed.pid == 4321


@pytest.mark.parametrize("fault", ["foreign", "root-recycled", "root-exited", "worker-recycled", "access-denied"])
def test_windows_service_identity_rejects_unowned_or_changed_processes(
    monkeypatch, managed_process_stub_factory, windows_process_tree_stub, fault,
) -> None:
    tree = windows_process_tree_stub
    root = tree.add_process(4321, parent_pid=None, starttime=1.0)
    tree.add_process(5001, parent_pid=None if fault == "foreign" else 4321, starttime=2.0)
    monkeypatch.setattr(os, "name", "nt")
    managed = ManagedProcess(managed_process_stub_factory(timeout_once=False), _LogFile())
    if fault == "root-recycled":
        root.starttime = 9.0
    elif fault == "root-exited":
        root.alive = False
    elif fault in {"worker-recycled", "access-denied"}:
        def get_process(pid):
            if pid == 5001:
                if fault == "access-denied":
                    raise psutil.AccessDenied(pid)
                return SimpleNamespace(create_time=lambda: 3.0)
            return tree.get_process(pid)
        monkeypatch.setattr(psutil, "Process", get_process)
    with pytest.raises(RuntimeError, match="不属于|已被替换|根已退出|已变化|创建时间失败") as caught:
        managed.confirm_service_identity(5001)
    assert managed.pid == 4321
    if fault == "access-denied":
        assert isinstance(caught.value.__cause__, psutil.AccessDenied)


@pytest.mark.skipif(os.name != "posix", reason="POSIX 服务须仍与原进程组根同 PID")
def test_posix_service_identity_keeps_single_process_contract(managed_process_stub_factory) -> None:
    managed = ManagedProcess(managed_process_stub_factory(timeout_once=False), _LogFile())
    with pytest.raises(RuntimeError, match="与受管进程根不匹配"):
        managed.confirm_service_identity(5001)
    assert managed.pid == 4321
    managed.confirm_service_identity(4321)
    managed.confirm_service_identity(4321)
    assert managed.pid == 4321 and managed.is_alive()


@pytest.mark.parametrize("adopted", [False, True])
def test_native_shutdown_transport_is_requested_once_and_still_waits_for_owned_exit(
    monkeypatch: pytest.MonkeyPatch, managed_process_stub_factory, adopted: bool,
) -> None:
    calls: list[str] = []
    if adopted:
        handle = AdoptedManagedProcess(pid=4321, shutdown_request=lambda: calls.append("request"))
        monkeypatch.setattr(AdoptedManagedProcess, "_close_target_is_alive", lambda self: "wait" not in calls)
        def wait_closed(*_args, **_kwargs):
            calls.append("wait")
            return True
        monkeypatch.setattr("app.gateway.runtime.process_lifecycle._wait_until_closed", wait_closed)
    else:
        process = managed_process_stub_factory(timeout_once=False)
        handle = ManagedProcess(process, _LogFile(), shutdown_request=lambda: calls.append("request"))
        monkeypatch.setattr(ManagedProcess, "_close_target_is_alive", lambda self: "wait" not in calls)
        monkeypatch.setattr(ManagedProcess, "_wait_closed", lambda self, timeout: calls.append("wait"))
    handle.request_terminate()
    handle.close()
    assert calls == ["request", "wait"]


@pytest.mark.parametrize("adopted", [False, True])
def test_native_shutdown_rejection_is_not_converted_to_os_kill(
    monkeypatch: pytest.MonkeyPatch, managed_process_stub_factory, adopted: bool,
) -> None:
    failure = RuntimeError("original-native-shutdown-rejected")
    def reject():
        raise failure
    if adopted:
        handle = AdoptedManagedProcess(pid=4321, shutdown_request=reject)
        monkeypatch.setattr(AdoptedManagedProcess, "_close_target_is_alive", lambda self: True)
    else:
        handle = ManagedProcess(managed_process_stub_factory(timeout_once=False), _LogFile(), shutdown_request=reject)
        monkeypatch.setattr(ManagedProcess, "_close_target_is_alive", lambda self: True)
        monkeypatch.setattr(ManagedProcess, "_kill_group", lambda self: pytest.fail("未接受关闭请求，不得隐式强杀"))
    with pytest.raises(RuntimeError) as caught:
        handle.close()
    assert caught.value is failure
