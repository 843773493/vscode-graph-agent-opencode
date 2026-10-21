from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from app.gateway.runtime import process_groups


def test_posix_process_snapshot_uses_creation_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(
        create_time=lambda: 1234.5,
        status=lambda: psutil.STATUS_SLEEPING,
        is_running=lambda: True,
    )
    monkeypatch.setattr(psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(process_groups.os, "getsid", lambda _pid: 4321)

    assert process_groups._posix_process_stat(4321) == ("S", 4321, 4321, 1234.5)


def test_posix_missing_process_has_no_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_process(pid: int) -> None:
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", missing_process)

    assert process_groups._posix_process_stat(4321) is None


def test_posix_process_query_errors_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied_process(pid: int) -> None:
        raise psutil.AccessDenied(pid)

    monkeypatch.setattr(psutil, "Process", denied_process)

    with pytest.raises(RuntimeError, match="读取 POSIX 进程身份失败"):
        process_groups._posix_process_stat(4321)


def test_posix_process_group_members_enumerate_only_matching_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def vanished_process_status() -> str:
        raise psutil.NoSuchProcess(103)

    processes = [
        100,
        101,
        102,
        103,
    ]

    def process_status(member_pid: int) -> str:
        if member_pid == 100:
            return psutil.STATUS_ZOMBIE
        if member_pid == 102:
            return psutil.STATUS_SLEEPING
        if member_pid == 103:
            return vanished_process_status()
        raise AssertionError(f"非目标进程组不读取状态: pid={member_pid}")

    monkeypatch.setattr(psutil, "pids", lambda: processes)
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda member_pid: SimpleNamespace(
            status=lambda: process_status(member_pid)
        ),
    )

    def process_group(member_pid: int) -> int:
        if member_pid == 103:
            raise ProcessLookupError
        return 4321 if member_pid != 101 else 9999

    monkeypatch.setattr(process_groups.os, "getpgid", process_group)

    assert process_groups._posix_process_group_members(4321) == [
        (100, "Z"),
        (102, "S"),
    ]


def test_posix_process_group_member_query_errors_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied_status() -> str:
        raise psutil.AccessDenied(100)

    monkeypatch.setattr(psutil, "pids", lambda: [100])
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda _pid: SimpleNamespace(status=denied_status),
    )
    monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 4321)

    with pytest.raises(RuntimeError, match="读取 POSIX 进程组成员状态失败"):
        process_groups._posix_process_group_members(4321)


def test_posix_capture_requires_a_new_session_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_groups.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_groups,
        "_posix_process_stat",
        lambda _pid: ("S", 4321, 4321, 1234.5),
    )
    assert process_groups._capture_posix_group_starttime(4321) == 1234.5


def test_live_posix_leader_proves_group_alive_without_member_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_groups.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_groups,
        "_posix_process_stat",
        lambda _pid: ("S", 4321, 4321, 1234.5),
    )
    monkeypatch.setattr(process_groups.os, "getsid", lambda _pid: 4321)
    monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(
        process_groups,
        "_posix_process_group_members",
        lambda _pid: pytest.fail("存活 leader 已证明进程组状态"),
    )

    assert process_groups._posix_group_is_alive(4321, 1234.5) is True


def test_zombie_posix_group_with_only_zombie_members_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_groups.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_groups,
        "_posix_process_stat",
        lambda _pid: ("Z", 4321, 4321, 1234.5),
    )
    monkeypatch.setattr(process_groups.os, "getsid", lambda _pid: 4321)
    monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(process_groups.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(
        process_groups,
        "_posix_process_group_members",
        lambda _pid: [(4321, "Z"), (9000, "Z")],
    )

    assert process_groups._posix_group_is_alive(4321, 1234.5) is False


def test_zombie_posix_leader_keeps_group_alive_for_active_session_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_groups.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_groups,
        "_posix_process_stat",
        lambda _pid: ("Z", 4321, 4321, 1234.5),
    )
    monkeypatch.setattr(process_groups.os, "getsid", lambda _pid: 4321)
    monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(process_groups.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(
        process_groups,
        "_posix_process_group_members",
        lambda _pid: [(4321, "Z"), (9000, "S")],
    )

    assert process_groups._posix_group_is_alive(4321, 1234.5) is True


def test_replaced_posix_leader_is_rejected_before_group_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_groups.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_groups,
        "_posix_process_stat",
        lambda _pid: ("S", 4321, 4321, 2345.6),
    )
    monkeypatch.setattr(
        process_groups.os,
        "killpg",
        lambda *_args: pytest.fail("不得探测已被替换 leader 的进程组"),
    )

    with pytest.raises(RuntimeError, match="leader 已被替换"):
        process_groups._posix_group_is_alive(4321, 1234.5)


def test_posix_group_member_from_another_session_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_groups.sys, "platform", "darwin")
    monkeypatch.setattr(process_groups, "_posix_process_stat", lambda _pid: None)
    monkeypatch.setattr(process_groups.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(process_groups.os, "getsid", lambda _pid: 9999)
    monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 4321)
    monkeypatch.setattr(
        process_groups,
        "_posix_process_group_members",
        lambda _pid: [(9000, "S")],
    )

    with pytest.raises(RuntimeError, match="无法证明进程属于原 owned group"):
        process_groups._posix_group_is_alive(4321, 1234.5)


def test_posix_group_member_that_moves_during_scan_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_groups.sys, "platform", "darwin")
    monkeypatch.setattr(process_groups, "_posix_process_stat", lambda _pid: None)
    monkeypatch.setattr(process_groups.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(process_groups.os, "getpgid", lambda _pid: 9999)
    monkeypatch.setattr(
        process_groups,
        "_posix_process_group_members",
        lambda _pid: [(9000, "S")],
    )

    assert process_groups._posix_group_is_alive(4321, 1234.5) is False


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc 进程组快速路径")
def test_live_leader_proves_group_alive_without_scanning_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_groups,
        "_linux_process_stat",
        lambda _pid: ("S", 4321, 4321, 100),
    )
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda _path: pytest.fail("存活的 leader 已证明进程组状态"),
    )

    assert process_groups._posix_group_is_alive(4321, 100) is True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc 进程组快速路径")
def test_missing_group_returns_without_scanning_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_group(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(process_groups, "_linux_process_stat", lambda _pid: None)
    monkeypatch.setattr(
        process_groups.os,
        "killpg",
        missing_group,
    )
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda _path: pytest.fail("空进程组无需扫描 /proc"),
    )

    assert process_groups._posix_group_is_alive(4321, 100) is False


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc 进程组成员核实")
def test_zombie_leader_still_checks_live_group_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def process_stat(pid: int) -> tuple[str, int, int, int] | None:
        if pid == 4321:
            return "Z", 4321, 4321, 100
        if pid == 9000:
            return "S", 4321, 4321, 200
        return None

    monkeypatch.setattr(process_groups, "_linux_process_stat", process_stat)
    monkeypatch.setattr(process_groups.os, "killpg", lambda _pid, _signal: None)
    monkeypatch.setattr(Path, "iterdir", lambda _path: [Path("/proc/9000")])

    assert process_groups._posix_group_is_alive(4321, 100) is True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc 进程组身份核实")
def test_replaced_leader_is_rejected_before_group_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_groups,
        "_linux_process_stat",
        lambda _pid: ("S", 4321, 4321, 200),
    )
    monkeypatch.setattr(
        process_groups.os,
        "killpg",
        lambda *_: pytest.fail("不得探测已被替换 leader 的进程组"),
    )

    with pytest.raises(RuntimeError, match="leader 已被替换"):
        process_groups._posix_group_is_alive(4321, 100)
