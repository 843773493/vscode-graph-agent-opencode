from pathlib import Path
from types import SimpleNamespace

import pytest

from app.gateway.runtime.ssh_process import (
    SshPortForwardSpec,
    _effective_forward_config,
    start_ssh_port_forward_process,
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

    managed, _, _ = start_ssh_port_forward_process(
        host="server.example.com",
        port=22,
        username="developer",
        private_key_path=None,
        ssh_config_host="development-alias",
        forward=SshPortForwardSpec(
            direction="L",
            listen_address="127.0.0.1",
            listen_port=41234,
            destination_address="127.0.0.1",
            destination_port=5173,
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


def test_ssh_config_isolates_forward_rules_and_keeps_connection_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.gateway.runtime.ssh_process.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="hostname server.example.com\nproxyjump bastion\nidentityfile /tmp/key with space\nlocalforward 3000 127.0.0.1:3000\nremoteforward 10808 127.0.0.1:10808\n", stderr="",
    ))
    configuration = _effective_forward_config(host="server.example.com", port=22, username="developer",
                                            private_key_path=None, ssh_config_host="development-alias")
    assert 'localforward' not in configuration and 'remoteforward' not in configuration
    assert 'proxyjump bastion' in configuration and 'identityfile "/tmp/key with space"' in configuration


def test_ipv6_forward_arguments_keep_brackets() -> None:
    assert SshPortForwardSpec("R", "::1", 8000, "::1", 3000).argument() == "[::1]:8000:[::1]:3000"


def test_forward_snapshot_does_not_override_proxyjump_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import shutil
    import subprocess

    if shutil.which("ssh") is None:
        pytest.skip("该场景需要 OpenSSH 配置解析器")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh/config").write_text(
        "Host bastion\n HostName 192.0.2.17\n User jumper\n Port 2201\n"
        " IdentityFile /tmp/jump-key\n"
        "Host destination\n HostName 192.0.2.18\n LocalForward 10808 localhost:10808\n",
    )
    real_run = subprocess.run
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("app.gateway.runtime.ssh_process.subprocess.run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="hostname 192.0.2.18\nuser target-user\nport 22\nproxyjump bastion\nlocalforward 10808 localhost:10808\n", stderr="",
    ))
    generated = tmp_path / "forward.ssh-config"
    generated.write_text(_effective_forward_config(host="192.0.2.18", port=22, username="target-user",
                                                   private_key_path=None, ssh_config_host="destination"))
    jump = real_run(["ssh", "-G", "-F", str(generated), "bastion"], capture_output=True, text=True, timeout=5, check=True).stdout
    assert "hostname 192.0.2.17\n" in jump and "user jumper\n" in jump and "port 2201\n" in jump
    assert "identityfile /tmp/jump-key\n" in jump and "proxyjump bastion" not in jump
    target = real_run(["ssh", "-G", "-F", str(generated), "destination"], capture_output=True, text=True, timeout=5, check=True).stdout
    assert "hostname 192.0.2.18\n" in target and "proxyjump bastion\n" in target
    assert "localforward " not in target
