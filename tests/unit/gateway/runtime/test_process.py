import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.gateway.runtime.process import (
    ManagedProcess,
    SshLocalForwardSpec,
    allocate_local_port_in_range,
    resolve_python_executable,
    ssh_tunnel_port_range_from_env,
    start_local_backend_process,
    start_workspace_ssh_port_forward_process,
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


def test_workspace_backend_has_bounded_connection_drain_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    process = _Process(timeout_once=False)

    def fake_spawn(command: list[str], **_: object) -> _Process:
        calls.append(command)
        return process

    monkeypatch.setenv("BOXTEAM_PYTHON_BIN", "/workspace/.venv/bin/python")
    monkeypatch.setattr(
        "app.gateway.runtime.process._spawn_logged_process",
        fake_spawn,
    )

    managed = start_local_backend_process(
        project_root=tmp_path,
        workspace_root=tmp_path / "workspace",
        port=41000,
        log_dir=tmp_path / "logs",
    )
    managed.detach()

    assert calls[0][-4:] == [
        "--log-level",
        "warning",
        "--timeout-graceful-shutdown",
        "2",
    ]


def test_workspace_ssh_port_forward_reuses_alias_and_long_lived_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    process = _Process(timeout_once=False)
    monkeypatch.setattr(
        "app.gateway.runtime.process.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="hostname server.example.com\nproxyjump bastion\n",
            stderr="",
        ),
    )

    def fake_spawn(command: list[str], **_: object) -> _Process:
        commands.append(command)
        return process

    monkeypatch.setattr(
        "app.gateway.runtime.process._spawn_logged_process",
        fake_spawn,
    )

    managed, _ = start_workspace_ssh_port_forward_process(
        host="server.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="development-alias",
        forward=SshLocalForwardSpec(
            local_port=41234,
            remote_host="127.0.0.1",
            remote_port=5173,
        ),
        log_dir=tmp_path / "logs",
        forward_id="pf_test",
    )
    managed.detach()

    command = commands[0]
    assert command[-1] == "development-alias"
    assert "-N" in command
    assert "-T" in command
    assert "127.0.0.1:41234:127.0.0.1:5173" in command
    assert "ExitOnForwardFailure=yes" in command
    assert "ServerAliveInterval=15" in command
    assert "ServerAliveCountMax=3" in command


def test_workspace_ssh_port_forward_rejects_configured_local_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.gateway.runtime.process.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "hostname server.example.com\n"
                "proxyjump bastion\n"
                "localforward 3000 [127.0.0.1]:3000\n"
            ),
            stderr="",
        ),
    )

    with pytest.raises(ValueError, match="专用 Host 别名"):
        start_workspace_ssh_port_forward_process(
            host="server.example.com",
            port=22,
            username="developer",
            private_key_path=None,
            ssh_config_host="development-alias",
            forward=SshLocalForwardSpec(
                local_port=41234,
                remote_host="127.0.0.1",
                remote_port=5173,
            ),
            log_dir=tmp_path / "logs",
            forward_id="pf_test",
        )
