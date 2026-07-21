from pathlib import Path
from typing import Literal

from tests.e2e.gateway.gateway_target import (
    GatewaySshTarget,
    build_remote_pair_command,
    ssh_command,
)


def _target(
    tmp_path: Path,
    platform: Literal["linux", "windows"],
) -> GatewaySshTarget:
    return GatewaySshTarget(
        target_id=f"mock-{platform}",
        platform=platform,
        host="target.example.test",
        ssh_port=22,
        username="developer",
        private_key=tmp_path / "id_ed25519",
        known_hosts_path=tmp_path / "known_hosts",
        repository_path=("C:\\boxteam-dev\\repository" if platform == "windows" else "/opt/boxteam-dev/repository"),
        python_environment=("C:\\boxteam-dev\\repository\\.venv" if platform == "windows" else "/opt/boxteam-dev/repository/.venv"),
    )


def test_linux_target_pair_command_uses_target_repository(tmp_path: Path) -> None:
    command = build_remote_pair_command(
        _target(tmp_path, "linux"),
        remote_boxteam_home="/tmp/boxteams-dev",
    )
    assert "cd /opt/boxteam-dev/repository" in command
    assert "/opt/boxteam-dev/repository/.venv/bin/python" in command


def test_windows_target_pair_command_uses_powershell_and_scripts_python(
    tmp_path: Path,
) -> None:
    command = build_remote_pair_command(
        _target(tmp_path, "windows"),
        remote_boxteam_home="C:\\boxteams-dev",
    )
    assert command.startswith("powershell -NoProfile")
    assert "Scripts\\python.exe" in command
    assert "/bin/python" not in command


def test_ssh_command_requires_preinstalled_host_key(tmp_path: Path) -> None:
    command = ssh_command(_target(tmp_path, "linux"), "echo ready")
    assert "StrictHostKeyChecking=yes" in command
    assert "StrictHostKeyChecking=accept-new" not in command
