from pathlib import Path
from types import SimpleNamespace

import pytest

from app.gateway.runtime.ssh_process import (
    SshLocalForwardSpec,
    start_workspace_ssh_port_forward_process,
)


def test_workspace_ssh_port_forward_reuses_alias_and_long_lived_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    managed_process_stub_factory,
) -> None:
    commands: list[list[str]] = []
    process = managed_process_stub_factory(timeout_once=False)
    monkeypatch.setattr(
        "app.gateway.runtime.ssh_process.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="hostname server.example.com\nproxyjump bastion\n",
            stderr="",
        ),
    )

    def fake_spawn(command: list[str], **_: object) -> object:
        commands.append(command)
        return process

    monkeypatch.setattr(
        "app.gateway.runtime.ssh_process._spawn_logged_process",
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
        "app.gateway.runtime.ssh_process.subprocess.run",
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
