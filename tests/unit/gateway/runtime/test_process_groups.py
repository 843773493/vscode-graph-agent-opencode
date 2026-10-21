from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.gateway.runtime import process_groups


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
