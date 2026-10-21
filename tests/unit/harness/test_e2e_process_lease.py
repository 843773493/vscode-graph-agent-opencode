from __future__ import annotations

import os
from pathlib import Path

import psutil
import pytest

from tests.support import e2e_test_control
from tests.support.e2e_test_control import E2EProcessLease


def test_process_start_time_uses_cross_platform_process_api() -> None:
    start_time = e2e_test_control._process_start_time(os.getpid())

    assert start_time is not None
    assert isinstance(start_time, float)


def test_process_start_time_returns_none_when_process_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_process(pid: int) -> psutil.Process:
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(e2e_test_control.psutil, "Process", missing_process)

    assert e2e_test_control._process_start_time(987654321) is None


def test_e2e_process_lease_rejects_live_owner_and_reclaims_reused_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_pid = os.getpid()
    start_times = {owner_pid: 100.25}
    checked_ports: list[tuple[int, str]] = []
    monkeypatch.setattr(
        e2e_test_control,
        "_process_start_time",
        lambda pid: start_times.get(pid),
    )
    monkeypatch.setattr(
        e2e_test_control,
        "ensure_local_port_available",
        lambda port, *, service_name: checked_ports.append((port, service_name)),
    )
    lease_path = tmp_path / "runtime" / "process-lease.json"
    original = E2EProcessLease(lease_path, (33001,))
    original.acquire()

    with pytest.raises(RuntimeError, match="租约仍有效"):
        E2EProcessLease(lease_path, (33001,)).acquire()

    start_times[owner_pid] = 101.5
    replacement = E2EProcessLease(lease_path, (33001,))
    replacement.acquire()
    with pytest.raises(RuntimeError, match="owner 变化"):
        original.release()

    replacement.release()

    assert not lease_path.exists()
    assert [port for port, _ in checked_ports] == [33001, 33001, 33001, 33001]
    assert all("E2E process lease" in service for _, service in checked_ports)
