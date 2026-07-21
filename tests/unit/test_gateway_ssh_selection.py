from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.schemas import AddSshWorkspaceRequest
from app.gateway.ssh_command import build_ssh_command
from app.gateway.ssh_config import list_user_ssh_hosts, resolve_user_ssh_host


@pytest.fixture
def ssh_config_files(tmp_path: Path) -> Path:
    included_path = tmp_path / "included.conf"
    included_path.write_text(
        "Host jump\n  HostName jump.example.com\n  User ops\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config"
    config_path.write_text(
        "\n".join(
            [
                "Host dev",
                "  HostName dev.example.com",
                "  User developer",
                "Include " + str(included_path),
                "Host *.internal",
                "  User ignored-pattern",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


@pytest.fixture
def mock_ssh_g(monkeypatch: pytest.MonkeyPatch):
    options = {
        "dev": "hostname dev.example.com\nuser developer\nport 2222\n",
        "jump": "hostname jump.example.com\nuser ops\nport 22\n",
    }

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        alias = command[-1]
        return subprocess.CompletedProcess(command, 0, options[alias], "")

    monkeypatch.setattr("app.gateway.ssh_config.subprocess.run", run)


def test_user_ssh_config_lists_concrete_hosts_and_includes(
    ssh_config_files: Path,
    mock_ssh_g: None,
):
    hosts = list_user_ssh_hosts(ssh_config_files)

    assert [(host.alias, host.hostname, host.port, host.username) for host in hosts] == [
        ("dev", "dev.example.com", 2222, "developer"),
        ("jump", "jump.example.com", 22, "ops"),
    ]
    assert resolve_user_ssh_host("dev", ssh_config_files).hostname == "dev.example.com"
    with pytest.raises(ValueError, match="不存在 Host"):
        resolve_user_ssh_host("missing", ssh_config_files)


def test_ssh_command_preserves_config_alias_and_explicit_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    known_hosts_path = tmp_path / "known_hosts"
    monkeypatch.setenv(
        "BOXTEAM_GATEWAY_SSH_KNOWN_HOSTS_FILE",
        str(known_hosts_path),
    )
    alias_command = build_ssh_command(
        host="resolved.example.com",
        port=2222,
        username="developer",
        private_key_path=None,
        ssh_config_host="dev",
        remote_command="pwd",
    )
    explicit_command = build_ssh_command(
        host="remote.example.com",
        port=2200,
        username="ops",
        private_key_path="~/.ssh/id_ed25519",
        ssh_config_host=None,
    )

    assert alias_command[-2:] == ["dev", "pwd"]
    assert "-i" not in alias_command
    assert "StrictHostKeyChecking=accept-new" in alias_command
    assert "BatchMode=yes" in alias_command
    assert f"UserKnownHostsFile={known_hosts_path}" in alias_command
    assert explicit_command[-1] == "ops@remote.example.com"
    assert "-i" in explicit_command
    assert "-p" in explicit_command


def test_add_ssh_request_requires_exactly_one_configured_connection_source():
    selected = AddSshWorkspaceRequest(
        ssh_config_host="dev",
        remote_gateway_port=8014,
    )
    assert selected.ssh_config_host == "dev"

    with pytest.raises(ValidationError, match="必须且只能选择"):
        AddSshWorkspaceRequest(remote_gateway_port=8014)
    with pytest.raises(ValidationError, match="必须且只能选择"):
        AddSshWorkspaceRequest(
            connection_workspace_id="gw_test",
            ssh_config_host="dev",
            remote_gateway_port=8014,
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AddSshWorkspaceRequest(
            host="dev.example.com",
            username="developer",
            private_key_path="~/.ssh/id_ed25519",
            remote_gateway_port=8014,
        )
