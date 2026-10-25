import base64
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from tests.e2e.system.gateway import gateway_target
from tests.e2e.system.gateway.gateway_target import (
    GatewaySshTarget,
    build_remote_pair_command,
    clear_remote_listeners,
    ssh_command,
    start_remote_gateway_via_ssh,
    stop_remote_gateway,
    wait_for_remote_gateway_ready,
)


def _target(
    tmp_path: Path,
    platform: Literal["linux", "macos", "windows"],
) -> GatewaySshTarget:
    return GatewaySshTarget(
        target_id=f"mock-{platform}",
        platform=platform,
        host="target.example.test",
        ssh_port=22,
        username="developer",
        private_key=tmp_path / "id_ed25519",
        known_hosts_path=tmp_path / "known_hosts",
        repository_path=(
            "C:\\boxteam-dev\\repository"
            if platform == "windows"
            else "/opt/boxteam-dev/repository"
        ),
        python_environment=(
            "C:\\boxteam-dev\\repository\\.venv"
            if platform == "windows"
            else "/opt/boxteam-dev/repository/.venv"
        ),
    )


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_posix_target_pair_command_uses_target_repository(
    tmp_path: Path,
    platform: Literal["linux", "macos"],
) -> None:
    command = build_remote_pair_command(
        _target(tmp_path, platform),
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
    assert command.startswith("powershell.exe -NoProfile")
    assert "Scripts\\python.exe" in command
    assert "/bin/python" not in command
    # 联邦 owner 会替换占位符；参数必须仍在远端 PowerShell 的脚本引号内。
    import shlex

    arguments = shlex.split(
        command.replace(
            "{pairing_args}", "--connection-id test:peer --peer-gateway-id peer --json"
        )
    )
    assert arguments[-1].endswith(
        "--connection-id test:peer --peer-gateway-id peer --json"
    )
    assert arguments[-2] == "-Command"


def test_windows_target_pair_command_escapes_literal_paths(tmp_path: Path) -> None:
    target = replace(
        _target(tmp_path, "windows"),
        repository_path=r"C:\boxteam dev\O'Neil\repository",
        python_environment=r"C:\boxteam dev\O'Neil\repository\.venv",
    )

    command = build_remote_pair_command(
        target,
        remote_boxteam_home=r"C:\boxteams dev\O'Neil\.boxteam-home",
    )

    assert "Set-Location -LiteralPath 'C:\\boxteam dev\\O''Neil\\repository'" in command
    assert "$env:BOXTEAM_HOME = 'C:\\boxteams dev\\O''Neil\\.boxteam-home'" in command
    assert "Scripts\\python.exe" in command


def test_ssh_command_requires_preinstalled_host_key(tmp_path: Path) -> None:
    command = ssh_command(_target(tmp_path, "linux"), "echo ready")
    assert "StrictHostKeyChecking=yes" in command
    assert "StrictHostKeyChecking=accept-new" not in command


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_posix_gateway_start_uses_platform_runtime_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: Literal["linux", "macos"],
) -> None:
    target = _target(tmp_path, platform)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        gateway_target,
        "clear_remote_listeners",
        lambda target_arg, ports: captured.update(target=target_arg, ports=ports),
    )
    monkeypatch.setattr(
        gateway_target,
        "run_ssh_command",
        lambda target_arg, command, *, timeout: (
            captured.update(command=command, timeout=timeout)
            or subprocess.CompletedProcess([], 0, "24680\n", "")
        ),
    )
    monkeypatch.setattr(
        gateway_target,
        "wait_for_remote_gateway_ready",
        lambda **options: captured.update(readiness=options),
    )

    process_id = start_remote_gateway_via_ssh(
        target=target,
        remote_workspace_path="/tmp/gateway-workspace",
        remote_gateway_port=24814,
        remote_boxteam_home="/tmp/gateway-home",
    )

    assert process_id == "24680"
    assert captured["target"] == target
    assert captured["ports"] == [24814, 8010, 8012, 8015]
    assert captured["timeout"] == 20
    assert captured["readiness"] == {
        "target": target,
        "remote_gateway_port": 24814,
        "process_id": "24680",
        "remote_boxteam_home": "/tmp/gateway-home",
    }
    command = str(captured["command"])
    assert "nohup" in command
    assert "-m uvicorn app.gateway.main:app" in command
    assert "BOXTEAM_GATEWAY_ROOT=/tmp/gateway-home/state/gateway" in command
    assert "/dev/null" in command
    assert "-EncodedCommand" not in command
    if platform == "linux":
        assert "BOXTEAM_NODE_BIN=/usr/local/bin/node" in command
        assert "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium" in command
    else:
        assert "node_executable=$(command -v node)" in command
        assert 'BOXTEAM_NODE_BIN="$node_executable"' in command
        assert "/usr/local/bin/node" not in command
        assert "/usr/bin/chromium" not in command


def _decode_powershell_command(command: str) -> str:
    arguments = command.split()
    encoded_index = arguments.index("-EncodedCommand") + 1
    return base64.b64decode(arguments[encoded_index]).decode("utf-16-le")


def test_windows_gateway_start_uses_windows_paths_and_literal_safe_encoding(
    tmp_path: Path,
) -> None:
    target = replace(
        _target(tmp_path, "windows"),
        repository_path=r"C:\boxteam dev\O'Neil\repository",
        python_environment=r"C:\boxteam dev\O'Neil\repository\.venv",
    )
    command = gateway_target._windows_gateway_start_command(
        target,
        remote_workspace_path=r"C:\test output\workspace\gateway",
        remote_gateway_port=24814,
        remote_boxteam_home=r"C:\test output\O'Neil\.boxteam-home",
    )

    assert command.startswith(
        "powershell.exe -NoLogo -NoProfile -NonInteractive -OutputFormat Text"
    )
    script = _decode_powershell_command(command)
    assert "$RepositoryRoot = 'C:\\boxteam dev\\O''Neil\\repository'" in script
    assert (
        "$Python = 'C:\\boxteam dev\\O''Neil\\repository\\.venv\\Scripts\\python.exe'"
        in script
    )
    assert "$GatewayArguments = @('-m', 'uvicorn', 'app.gateway.main:app'" in script
    assert "'--port', '24814', '--log-level', 'warning')" in script
    assert "-ArgumentList $GatewayArguments" in script
    assert "Get-Command node.exe -ErrorAction Stop" in script
    assert "System.Text.UTF8Encoding]::new($false)" in script
    assert "/dev/null" not in script


def test_windows_listener_cleanup_targets_only_distinct_requested_ports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(tmp_path, "windows")
    captured: dict[str, object] = {}

    def fake_run_ssh_command(
        target_arg: GatewaySshTarget,
        command: str,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        captured.update(target=target_arg, command=command, timeout=timeout)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(gateway_target, "run_ssh_command", fake_run_ssh_command)
    clear_remote_listeners(target, [24814, 8010, 24814])

    assert captured["target"] == target
    assert captured["timeout"] == 20
    script = _decode_powershell_command(str(captured["command"]))
    assert "foreach ($Port in @(8010, 24814))" in script
    assert "Get-NetTCPConnection -State Listen -LocalPort $Port" in script
    assert "Stop-Process -Id $_ -Force" in script


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_posix_listener_cleanup_distinguishes_empty_ports_from_command_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: Literal["linux", "macos"],
) -> None:
    target = _target(tmp_path, platform)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        gateway_target,
        "run_ssh_command",
        lambda target_arg, command, *, timeout: (
            captured.update(target=target_arg, command=command, timeout=timeout)
            or subprocess.CompletedProcess([], 0, "", "")
        ),
    )

    clear_remote_listeners(target, [24814, 8010, 24814])

    assert captured["target"] == target
    assert captured["timeout"] == 20
    command = str(captured["command"])
    assert "for port in 8010 24814" in command
    assert 'if [ "$status" -ne 1 ]; then exit "$status"; fi' in command
    assert 'kill "$pid"' in command
    assert "|| true" not in command


def test_windows_gateway_start_entrypoint_waits_for_the_started_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(tmp_path, "windows")
    captured: dict[str, object] = {}
    monkeypatch.setattr(gateway_target, "clear_remote_listeners", lambda *_args: None)
    monkeypatch.setattr(
        gateway_target,
        "run_ssh_command",
        lambda _target, command, *, timeout: (
            captured.update(command=command, timeout=timeout)
            or subprocess.CompletedProcess([], 0, "24680\n", "")
        ),
    )
    monkeypatch.setattr(
        gateway_target,
        "wait_for_remote_gateway_ready",
        lambda **options: captured.update(readiness=options),
    )

    process_id = start_remote_gateway_via_ssh(
        target=target,
        remote_workspace_path=r"C:\test output\workspace",
        remote_gateway_port=24814,
        remote_boxteam_home=r"C:\test output\boxteam-home",
    )

    assert process_id == "24680"
    assert captured["timeout"] == 20
    assert captured["readiness"] == {
        "target": target,
        "remote_gateway_port": 24814,
        "process_id": "24680",
        "remote_boxteam_home": r"C:\test output\boxteam-home",
    }
    assert "Start-Process" in _decode_powershell_command(str(captured["command"]))


def test_windows_gateway_readiness_probes_owned_pid_and_health_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(tmp_path, "windows")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        gateway_target,
        "ssh_command",
        lambda _target, command: captured.update(command=command) or ["ssh", command],
    )
    monkeypatch.setattr(
        gateway_target.subprocess,
        "run",
        lambda command, **_options: subprocess.CompletedProcess(command, 0, "", ""),
    )

    wait_for_remote_gateway_ready(
        target=target,
        remote_gateway_port=24814,
        process_id="24680",
        remote_boxteam_home=r"C:\test output\boxteam-home",
    )

    script = _decode_powershell_command(str(captured["command"]))
    assert "Get-Process -Id 24680" in script
    assert "Invoke-WebRequest" in script
    assert "http://127.0.0.1:24814/api/gateway/health" in script


def test_windows_gateway_cleanup_kills_the_owned_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(tmp_path, "windows")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        gateway_target,
        "ssh_command",
        lambda _target, command: captured.update(command=command) or ["ssh", command],
    )
    monkeypatch.setattr(
        gateway_target.subprocess,
        "run",
        lambda command, **_options: subprocess.CompletedProcess(command, 0, "", ""),
    )

    stop_remote_gateway(target, "24680")

    script = _decode_powershell_command(str(captured["command"]))
    assert "foreach ($TargetProcessId in @(24680))" in script
    assert "taskkill.exe /PID $TargetProcessId /T /F" in script
    assert "$ProcessId" not in script
    assert "Windows 进程树未退出" in script


def test_windows_gateway_cleanup_rejects_non_numeric_pid(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Windows 进程 ID 无效"):
        stop_remote_gateway(_target(tmp_path, "windows"), "24680; Remove-Item C:\\")


def test_posix_gateway_cleanup_waits_for_exit_and_surfaces_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target(tmp_path, "linux")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        gateway_target,
        "ssh_command",
        lambda _target, command: captured.update(command=command) or ["ssh", command],
    )
    monkeypatch.setattr(
        gateway_target.subprocess,
        "run",
        lambda command, **_options: subprocess.CompletedProcess(command, 0, "", ""),
    )

    stop_remote_gateway(target, "24680")

    command = str(captured["command"])
    assert "set -eu" in command
    assert 'kill -KILL "$target_pid"' in command
    assert "远端进程仍在运行" in command
    assert "|| true" not in command


def test_posix_gateway_cleanup_rejects_non_numeric_pid(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="POSIX 进程 ID 无效"):
        stop_remote_gateway(_target(tmp_path, "macos"), "24680; rm -rf /")
