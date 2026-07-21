import socket
import subprocess
from pathlib import Path

import pytest

from app.gateway.runtime.process import (
    ManagedProcess,
    allocate_local_port_in_range,
    resolve_python_executable,
    ssh_tunnel_port_range_from_env,
)


class _LogFile:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Process:
    pid = 4321

    def __init__(self, *, timeout_once: bool) -> None:
        self.returncode = None
        self._timeout_once = timeout_once
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.wait_calls += 1
        if self._timeout_once and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("test", timeout)
        self.returncode = 0
        return 0


def test_resolve_python_executable_preserves_venv_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", "/workspace/.venv/bin/python")

    assert resolve_python_executable(Path("/workspace")) == Path(
        "/workspace/.venv/bin/python"
    )


def test_resolve_python_executable_rejects_relative_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", ".venv/bin/python")

    with pytest.raises(ValueError, match="必须是绝对路径"):
        resolve_python_executable(Path("/workspace"))


def test_ssh_tunnel_port_range_reads_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MIN", "43000")
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MAX", "43010")

    assert ssh_tunnel_port_range_from_env() == (43000, 43010)


def test_ssh_tunnel_port_range_rejects_invalid_order(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MIN", "43010")
    monkeypatch.setenv("BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MAX", "43000")

    with pytest.raises(ValueError, match="不能大于"):
        ssh_tunnel_port_range_from_env()


def test_allocate_local_port_in_range_skips_occupied_port():
    first_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        first_socket.bind(("127.0.0.1", 0))
        occupied_port = int(first_socket.getsockname()[1])
        free_port = None
        for candidate in (occupied_port - 1, occupied_port + 1):
            if candidate < 1 or candidate > 65535:
                continue
            probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe_socket.bind(("127.0.0.1", candidate))
                free_port = candidate
                break
            except OSError:
                continue
            finally:
                probe_socket.close()
        if free_port is None:
            pytest.skip("没有找到相邻可用端口用于范围分配测试")

        assert (
            allocate_local_port_in_range(
                min(occupied_port, free_port),
                max(occupied_port, free_port),
            )
            == free_port
        )
    finally:
        first_socket.close()


def test_managed_process_closes_posix_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(timeout_once=False)
    log_file = _LogFile()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "app.gateway.runtime.process.os.killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    ManagedProcess(process=process, log_file=log_file).close()

    assert signals == [(4321, 15)]
    assert log_file.closed is True


def test_managed_process_kills_group_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(timeout_once=True)
    log_file = _LogFile()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "app.gateway.runtime.process.os.killpg",
        lambda pid, sent_signal: signals.append((pid, sent_signal)),
    )

    ManagedProcess(process=process, log_file=log_file).close(timeout_seconds=0.01)

    assert signals == [(4321, 15), (4321, 9)]
    assert process.wait_calls == 2
    assert log_file.closed is True
