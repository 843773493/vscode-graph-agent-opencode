from __future__ import annotations

import errno
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.gateway import ssh_config
from app.gateway.config import ConfiguredRemoteGateway, GatewayConfig
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.ssh_command import build_ssh_command
from app.gateway.ssh_config import (
    UserSshHost,
    list_user_ssh_hosts,
    resolve_user_ssh_host,
)
from app.gateway.ssh_connections import resolve_ssh_connection_request
from app.schemas.gateway.ssh import AddRemoteGatewayRequest


@pytest.fixture
def ssh_config_with_includes(tmp_path: Path) -> Path:
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
def stub_ssh_config_resolution(monkeypatch: pytest.MonkeyPatch) -> Mock:
    options = {
        "dev": "hostname dev.example.com\nuser developer\nport 2222\n",
        "jump": "hostname jump.example.com\nuser ops\nport 22\n",
    }

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        alias = command[-1]
        return subprocess.CompletedProcess(command, 0, options[alias], "")

    process = Mock(side_effect=run)
    monkeypatch.setattr("app.gateway.ssh_config.subprocess.run", process)
    return process


def test_user_ssh_config_lists_concrete_hosts_and_includes(
    ssh_config_with_includes: Path,
    stub_ssh_config_resolution: Mock,
):
    hosts = list_user_ssh_hosts(ssh_config_with_includes)

    assert [(host.alias, host.hostname, host.port, host.username) for host in hosts] == [
        ("dev", "dev.example.com", 2222, "developer"),
        ("jump", "jump.example.com", 22, "ops"),
    ]
    assert (
        resolve_user_ssh_host("dev", ssh_config_with_includes).hostname
        == "dev.example.com"
    )
    with pytest.raises(ValueError, match="不存在 Host"):
        resolve_user_ssh_host("missing", ssh_config_with_includes)


@pytest.fixture
def rejected_ssh_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> tuple[Path, str, Mock]:
    config = tmp_path / "config"
    config.write_text("Host dev\n  HostName example.invalid\n  User developer\n", encoding="utf-8")
    path = str(request.param)
    detail = f"{path} line 3: Bad configuration option: unknownoption"
    run = Mock(return_value=subprocess.CompletedProcess(["ssh", "-G", "dev"], 255,
                                                        f"stdout: {detail}", f"stderr: {detail}"))
    monkeypatch.setattr("app.gateway.ssh_config.subprocess.run", run)
    return config, detail, run


@pytest.mark.parametrize("rejected_ssh_config", [
    "/private/ssh-config-location-marker/config", r"C:\Users\ssh-config-location-marker\config",
    r"\\ssh-config-location-marker\share\config",
], indirect=True)
@pytest.mark.parametrize("entry", ["list", "resolve"])
def test_ssh_config_failure_keeps_raw_process_output_in_diagnostics(
    rejected_ssh_config: tuple[Path, str, Mock], entry: str, caplog: pytest.LogCaptureFixture,
) -> None:
    config, detail, run = rejected_ssh_config
    with pytest.raises(RuntimeError) as raised:
        if entry == "list":
            list_user_ssh_hosts(config)
        else:
            resolve_user_ssh_host("dev", config)
    assert str(raised.value) == "解析 SSH Host dev 失败: stage=effective_config, returncode=255"
    assert "ssh-config-location-marker" not in str(raised.value)
    assert f"stdout: {detail}" in caplog.text
    assert f"stderr: {detail}" in caplog.text
    run.assert_called_once_with(["ssh", "-G", "dev"], capture_output=True, text=True, timeout=10, check=False)


@pytest.fixture
def unreadable_ssh_config(
    ssh_config_with_includes: Path, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> tuple[Path, OSError | UnicodeError, Mock]:
    location, error_kind, native_path = request.param
    config = ssh_config_with_includes
    target = config if location == "root" else config.with_name("included.conf")
    error = (
        PermissionError(errno.EACCES, f"无法读取 {native_path}", str(target))
        if error_kind == "permission"
        else UnicodeDecodeError("utf-8", b"\xff", 0, 1, f"编码错误 {native_path}")
    )
    read_text = Path.read_text
    attempts = Mock()

    def read(path: Path, *args, **kwargs):
        if path == target:
            attempts()
            if attempts.call_count == 1:
                raise error
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    return config, error, attempts


@pytest.mark.parametrize("unreadable_ssh_config", [
    (location, error_kind, native_path)
    for location in ("root", "include")
    for error_kind in ("permission", "encoding")
    for native_path in (
        "/private/ssh-read-location-marker/config",
        r"C:\Users\ssh-read-location-marker\config",
        r"\\ssh-read-location-marker\share\config",
    )
], indirect=True)
@pytest.mark.parametrize("entry", ["list", "resolve"])
def test_ssh_config_read_failure_keeps_native_details_only_in_diagnostics(
    unreadable_ssh_config: tuple[Path, OSError | UnicodeError, Mock],
    stub_ssh_config_resolution: Mock, entry: str, caplog: pytest.LogCaptureFixture,
) -> None:
    config, original, attempts = unreadable_ssh_config
    with pytest.raises((OSError, UnicodeError)) as failed:
        if entry == "list":
            list_user_ssh_hosts(config)
        else:
            resolve_user_ssh_host("dev", config)
    expected = f"读取 SSH Host 配置失败: stage=config_read, type={type(original).__name__}"
    if isinstance(original, OSError):
        assert isinstance(failed.value, OSError)
        expected = f"[Errno {original.errno}] {expected}, errno={original.errno}"
        assert type(failed.value) is type(original)
        assert failed.value.errno == original.errno
        assert failed.value.filename is None
    assert str(failed.value) == expected
    assert "ssh-read-location-marker" not in str(failed.value)
    assert failed.value.__cause__ is original
    record, = [record for record in caplog.records if "stage=config_read" in record.message]
    assert record.exc_info is not None and record.exc_info[1] is original
    assert str(original) in caplog.text
    attempts.assert_called_once()
    stub_ssh_config_resolution.assert_not_called()
    # 同入口的显式重试重新读取，不能把失败当成空清单或丢失 Include。
    if entry == "list":
        assert [host.alias for host in list_user_ssh_hosts(config)] == ["dev", "jump"]
    else:
        assert resolve_user_ssh_host("dev", config).hostname == "dev.example.com"
    assert attempts.call_count == 2


def test_windows_ssh_include_keeps_backslashes_and_removes_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.gateway.ssh_config.os.name", "nt")

    tokens = ssh_config._split_config_line(
        r'Include "C:\Users\Jöhn Doe\.ssh\config.d\*.conf" # nested config'
    )

    assert tokens == ["Include", r"C:\Users\Jöhn Doe\.ssh\config.d\*.conf"]


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


def test_add_ssh_request_accepts_selection_or_complete_manual_connection():
    selected = AddRemoteGatewayRequest(
        ssh_config_host="dev",
        remote_gateway_port=8014,
    )
    assert selected.ssh_config_host == "dev"
    manual = AddRemoteGatewayRequest(
        name="GPU 主机",
        host="dev.example.com",
        port=2222,
        username="developer",
        private_key_path="~/.ssh/id_ed25519",
        remote_gateway_port=8014,
    )
    assert manual.host == "dev.example.com"
    assert manual.port == 2222
    resolved_manual = resolve_ssh_connection_request(
        manual,
        Mock(spec=GatewayWorkspaceRegistry),
    )
    assert resolved_manual.host == "dev.example.com"
    assert resolved_manual.username == "developer"
    assert resolved_manual.private_key_path == "~/.ssh/id_ed25519"

    with pytest.raises(ValidationError, match="必须且只能选择"):
        AddRemoteGatewayRequest(remote_gateway_port=8014)
    with pytest.raises(ValidationError, match="必须且只能选择"):
        AddRemoteGatewayRequest(
            connection_workspace_id="gw_test",
            ssh_config_host="dev",
            remote_gateway_port=8014,
        )
    with pytest.raises(ValidationError, match="必须提供主机、用户名和私钥路径"):
        AddRemoteGatewayRequest(
            host="dev.example.com",
            username="developer",
            remote_gateway_port=8014,
        )
    with pytest.raises(ValidationError, match="必须且只能选择"):
        AddRemoteGatewayRequest(
            ssh_config_host="dev",
            host="dev.example.com",
            username="developer",
            private_key_path="~/.ssh/id_ed25519",
            remote_gateway_port=8014,
        )


def test_ssh_config_selection_uses_pairing_command_from_gateway_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.gateway.ssh_connections.resolve_user_ssh_host",
        lambda alias: UserSshHost(
            alias=alias,
            hostname="127.0.0.1",
            port=22022,
            username="Administrator",
        ),
    )
    monkeypatch.setattr(
        "app.gateway.ssh_connections.load_gateway_config",
        lambda: GatewayConfig(
            workspaces=(
                ConfiguredRemoteGateway(
                    name="Windows Gateway",
                    host="127.0.0.1",
                    port=22022,
                    username="Administrator",
                    private_key_path="~/.ssh/a4500-windows-vm",
                    ssh_config_host="a4500-windows-vm",
                    remote_pair_command="python -m app.gateway.federation_pairing",
                ),
            )
        ),
    )

    resolved = resolve_ssh_connection_request(
        AddRemoteGatewayRequest(
            ssh_config_host="a4500-windows-vm",
            remote_gateway_port=8014,
        ),
        Mock(spec=GatewayWorkspaceRegistry),
    )

    assert resolved.ssh_config_host == "a4500-windows-vm"
    assert resolved.host == "127.0.0.1"
    assert resolved.port == 22022
    assert resolved.username == "Administrator"
    assert resolved.remote_pair_command == "python -m app.gateway.federation_pairing"
