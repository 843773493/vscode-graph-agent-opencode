from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.gateway.registry import GatewayWorkspaceRegistry, SshWorkspaceConnection
from app.gateway.remote_files import _run_remote_directory_query
from app.gateway.schemas import AddSshWorkspaceRequest
from app.gateway.ssh_command import build_ssh_command
from app.gateway.ssh_config import list_user_ssh_hosts, resolve_user_ssh_host
from app.gateway.ssh_workspace import register_ssh_workspace


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
    assert not any("StrictHostKeyChecking" in argument for argument in alias_command)
    assert "BatchMode=yes" in alias_command
    assert f"UserKnownHostsFile={known_hosts_path}" in alias_command
    assert explicit_command[-1] == "ops@remote.example.com"
    assert "-i" in explicit_command
    assert "-p" in explicit_command


def test_remote_directory_query_uses_selected_ssh_config_host(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_command: list[str] = []
    payload = {
        "path": "/home/dev/project",
        "parent_path": "/home/dev",
        "home_path": "/home/dev",
        "entries": [{"name": "src", "path": "/home/dev/project/src"}],
        "truncated": False,
        "limit": 120,
    }

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured_command.extend(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr("app.gateway.remote_files.subprocess.run", run)
    result = _run_remote_directory_query(
        SshWorkspaceConnection(
            host="resolved.example.com",
            port=2222,
            username="developer",
            private_key_path=None,
            remote_backend_host="127.0.0.1",
            remote_backend_port=8010,
            ssh_config_host="dev",
        ),
        "/home/dev/project",
        120,
    )

    assert "dev" in captured_command
    assert "-i" not in captured_command
    assert result.entries[0].name == "src"


def test_add_ssh_request_requires_exactly_one_configured_connection_source():
    selected = AddSshWorkspaceRequest(
        ssh_config_host="dev",
        remote_workspace_path="/home/dev/project",
    )
    assert selected.ssh_config_host == "dev"

    with pytest.raises(ValidationError, match="必须且只能选择"):
        AddSshWorkspaceRequest(remote_workspace_path="/home/dev/project")
    with pytest.raises(ValidationError, match="必须且只能选择"):
        AddSshWorkspaceRequest(
            connection_workspace_id="gw_test",
            ssh_config_host="dev",
            remote_workspace_path="/home/dev/project",
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AddSshWorkspaceRequest(
            host="dev.example.com",
            username="developer",
            private_key_path="~/.ssh/id_ed25519",
            remote_workspace_path="/home/dev/project",
        )


@pytest.mark.asyncio
async def test_register_ssh_workspace_rejects_backend_for_another_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeTunnel:
        def __init__(self) -> None:
            self.process = object()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    private_key = tmp_path / "id_ed25519"
    private_key.write_text("test", encoding="utf-8")
    tunnel = FakeTunnel()
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json")
    allocated_ports = iter([41000, 41001, 41002])
    monkeypatch.setattr(
        "app.gateway.ssh_workspace.allocate_ssh_tunnel_port",
        lambda: next(allocated_ports),
    )
    monkeypatch.setattr(
        "app.gateway.ssh_workspace.start_ssh_tunnel_process",
        lambda **_: tunnel,
    )

    async def wait_for_http_ok(*_: object) -> None:
        return None

    async def read_workspace_root(_: str) -> str:
        return "/srv/actual"

    monkeypatch.setattr(
        "app.gateway.ssh_workspace.wait_for_http_ok",
        wait_for_http_ok,
    )
    monkeypatch.setattr(
        "app.gateway.ssh_workspace.read_workspace_root",
        read_workspace_root,
    )

    with pytest.raises(ValueError, match="实际工作区与所选目录不一致"):
        await register_ssh_workspace(
            registry=registry,
            log_dir=tmp_path / "logs",
            name=None,
            host="remote.example.com",
            port=22,
            username="developer",
            private_key_path=str(private_key),
            ssh_config_host=None,
            remote_backend_host="127.0.0.1",
            remote_backend_port=8010,
            remote_workspace_path="/srv/selected",
            activate=False,
        )

    assert tunnel.closed is True
    assert registry.targets() == ()
