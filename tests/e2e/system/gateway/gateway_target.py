from __future__ import annotations

import base64
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from configs.boxteam import SSH_KEY_NAME, install_gateway_development_assets

READY_TIMEOUT_SECONDS = 60
TARGET_READY_TIMEOUT_SECONDS = 240


@dataclass(frozen=True, slots=True)
class GatewaySshTarget:
    target_id: str
    platform: Literal["linux", "macos", "windows"]
    host: str
    ssh_port: int
    username: str
    private_key: Path
    known_hosts_path: Path
    repository_path: str
    python_environment: str


@dataclass(frozen=True, slots=True)
class GatewayTargetE2EPaths:
    root: Path
    local_workspace: Path
    artifacts: Path
    remote_workspace: str
    remote_boxteam_home: str


def build_remote_pair_command(
    target: GatewaySshTarget,
    *,
    remote_boxteam_home: str,
) -> str:
    if target.platform == "windows":
        # TODO: 获得 VMware Windows 资源后执行真实联邦配对 E2E。
        python = _remote_python(target)
        gateway_root = _remote_join(
            target,
            remote_boxteam_home,
            "state",
            "gateway",
        )
        return (
            'powershell.exe -NoProfile -Command "'
            "$ErrorActionPreference = 'Stop'; Set-StrictMode -Version Latest; "
            f"Set-Location -LiteralPath {_powershell_quote(target.repository_path)}; "
            f"$env:BOXTEAM_HOME = {_powershell_quote(remote_boxteam_home)}; "
            f"$env:BOXTEAM_GATEWAY_ROOT = {_powershell_quote(gateway_root)}; "
            f"& {_powershell_quote(python)} -m app.gateway.federation_pairing "
            '{pairing_args}"'
        )
    return (
        f"cd {shlex.quote(target.repository_path)} && "
        f"BOXTEAM_HOME={shlex.quote(remote_boxteam_home)} "
        f"BOXTEAM_GATEWAY_ROOT={shlex.quote(_remote_join(target, remote_boxteam_home, 'state', 'gateway'))} "
        f"{shlex.quote(_remote_python(target))} "
        "-m app.gateway.federation_pairing"
    )


def _remote_join(target: GatewaySshTarget, root: str, *parts: str) -> str:
    path_type = PureWindowsPath if target.platform == "windows" else PurePosixPath
    return str(path_type(root).joinpath(*parts))


def _remote_python(target: GatewaySshTarget) -> str:
    if target.platform == "windows":
        return _remote_join(target, target.python_environment, "Scripts", "python.exe")
    return _remote_join(target, target.python_environment, "bin", "python")


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell_command(script: str) -> str:
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (
        "powershell.exe -NoLogo -NoProfile -NonInteractive -OutputFormat Text "
        f"-EncodedCommand {encoded_script}"
    )


def install_gateway_ssh_assets_for_e2e(workspace_root: Path) -> Path:
    project_root = Path.cwd().resolve()
    test_home = workspace_root.parent / "artifacts" / "home"
    install_gateway_development_assets(project_root=project_root, home=test_home)
    return test_home / ".ssh" / SSH_KEY_NAME


def clear_remote_listeners(
    target: GatewaySshTarget,
    ports: list[int],
) -> None:
    normalized_ports = sorted(set(ports))
    if not normalized_ports:
        return
    if any(not 1 <= port <= 65535 for port in normalized_ports):
        raise ValueError(f"远端监听端口超出有效范围: {normalized_ports}")
    if target.platform == "windows":
        script = "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "Set-StrictMode -Version Latest",
                "foreach ($Port in @(" + ", ".join(map(str, normalized_ports)) + ")) {",
                "  Get-NetTCPConnection -State Listen -LocalPort $Port |",
                "    Select-Object -ExpandProperty OwningProcess -Unique |",
                "    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction Stop }",
                "}",
            ]
        )
        run_ssh_command(target, _powershell_command(script), timeout=20)
        return
    port_values = " ".join(str(port) for port in normalized_ports)
    run_ssh_command(
        target,
        (
            "set -eu; "
            f"for port in {port_values}; do "
            "status=0; "
            'if pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN); then :; '
            'else status=$?; if [ "$status" -ne 1 ]; then exit "$status"; fi; pids=""; fi; '
            "for pid in $pids; do "
            'if ! kill "$pid" 2>/dev/null && kill -0 "$pid" 2>/dev/null; then '
            'echo "远端监听进程无法停止: $pid" >&2; exit 1; fi; '
            "done; done"
        ),
        timeout=20,
    )


def _posix_runtime_manifest_command(
    target: GatewaySshTarget, manifest_path: str
) -> str:
    """测试启动器在远端生成与源码 Launcher 相同形态的真实 manifest。"""
    script = """import json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
output = Path(sys.argv[2])
package = json.loads((root / "package.json").read_text(encoding="utf-8"))
manifest = {
    "schema_version": 1, "distribution": "source-development", "version": package["version"],
    "python_executable": sys.executable, "application_root": str(root),
    "config_resources": {name: str(root / "configs" / (name + ".jsonc")) for name in (
        "gateway_inline", "gateway_schema", "workspace_inline", "workspace_schema"
    )},
    "skill_resources": str(root / "resources" / "skills"), "web_assets": None,
    "chromium_executable": os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"),
    "node": {"source": "launcher", "executable": None},
}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
"""
    return " ".join(
        shlex.quote(value)
        for value in (
            f"{target.python_environment}/bin/python",
            "-c",
            script,
            target.repository_path,
            manifest_path,
        )
    )


def _windows_gateway_start_command(
    target: GatewaySshTarget,
    *,
    remote_workspace_path: str,
    remote_gateway_port: int,
    remote_boxteam_home: str,
) -> str:
    # TODO: 在原生 Windows 上通过 tests/integration/harness/test_windows_gateway_target.py 后再依赖此启动器。
    repository = target.repository_path
    python = _remote_python(target)
    config_root = _remote_join(target, remote_boxteam_home, "config")
    logs_root = _remote_join(target, remote_boxteam_home, "logs")
    gateway_root = _remote_join(target, remote_boxteam_home, "state", "gateway")
    runtime_manifest = _remote_join(
        target,
        remote_boxteam_home,
        "runtime",
        "runtime-manifest.json",
    )
    script = "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "Set-StrictMode -Version Latest",
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
            f"$RepositoryRoot = {_powershell_quote(repository)}",
            f"$Python = {_powershell_quote(python)}",
            f"$WorkspaceRoot = {_powershell_quote(remote_workspace_path)}",
            f"$BoxTeamHome = {_powershell_quote(remote_boxteam_home)}",
            f"$ConfigRoot = {_powershell_quote(config_root)}",
            f"$LogsRoot = {_powershell_quote(logs_root)}",
            f"$RuntimeManifest = {_powershell_quote(runtime_manifest)}",
            "foreach ($Path in @($BoxTeamHome, $WorkspaceRoot)) {",
            "  if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }",
            "}",
            "New-Item -ItemType Directory -Force -Path @($ConfigRoot, $LogsRoot, $WorkspaceRoot, (Split-Path -Parent $RuntimeManifest)) | Out-Null",
            "Copy-Item -LiteralPath (Join-Path $RepositoryRoot 'configs/gateway_inline.jsonc') -Destination (Join-Path $ConfigRoot 'gateway.jsonc') -Force",
            "Copy-Item -LiteralPath (Join-Path $RepositoryRoot 'configs/gateway_schema.jsonc') -Destination (Join-Path $ConfigRoot 'gateway_schema.jsonc') -Force",
            "Copy-Item -LiteralPath (Join-Path $RepositoryRoot 'configs/workspace_inline.jsonc') -Destination (Join-Path $ConfigRoot 'workspace.jsonc') -Force",
            "Copy-Item -LiteralPath (Join-Path $RepositoryRoot 'configs/workspace_schema.jsonc') -Destination (Join-Path $ConfigRoot 'workspace_schema.jsonc') -Force",
            "$Package = Get-Content -LiteralPath (Join-Path $RepositoryRoot 'package.json') -Raw | ConvertFrom-Json",
            "$Manifest = [ordered]@{",
            "  schema_version = 1",
            "  distribution = 'source-development'",
            "  version = $Package.version",
            "  python_executable = $Python",
            "  application_root = $RepositoryRoot",
            "  config_resources = [ordered]@{",
            "    gateway_inline = Join-Path $RepositoryRoot 'configs/gateway_inline.jsonc'",
            "    gateway_schema = Join-Path $RepositoryRoot 'configs/gateway_schema.jsonc'",
            "    workspace_inline = Join-Path $RepositoryRoot 'configs/workspace_inline.jsonc'",
            "    workspace_schema = Join-Path $RepositoryRoot 'configs/workspace_schema.jsonc'",
            "  }",
            "  skill_resources = Join-Path $RepositoryRoot 'resources/skills'",
            "  web_assets = $null",
            "  chromium_executable = [Environment]::GetEnvironmentVariable('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH')",
            "  node = @{ source = 'launcher'; executable = $null }",
            "}",
            "$ManifestJson = ConvertTo-Json -InputObject $Manifest -Depth 8",
            "[System.IO.File]::WriteAllText($RuntimeManifest, $ManifestJson + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))",
            "$NodeExecutable = (Get-Command node.exe -ErrorAction Stop).Source",
            "$env:BOXTEAM_HOME = $BoxTeamHome",
            f"$env:BOXTEAM_GATEWAY_ROOT = {_powershell_quote(gateway_root)}",
            "$env:BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT = $WorkspaceRoot",
            "$env:BOXTEAM_RUNTIME_MANIFEST = $RuntimeManifest",
            "$env:BOXTEAM_PYTHON_BIN = $Python",
            "$env:BOXTEAM_NODE_BIN = $NodeExecutable",
            "$env:PYTHONUNBUFFERED = '1'",
            (
                "$GatewayArguments = @('-m', 'uvicorn', 'app.gateway.main:app', '--host', "
                "'127.0.0.1', '--port', "
                f"'{remote_gateway_port}', '--log-level', 'warning')"
            ),
            'if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python executable not found: $Python" }',
            "$Process = Start-Process -FilePath $Python -ArgumentList $GatewayArguments -WorkingDirectory $RepositoryRoot -RedirectStandardOutput (Join-Path $LogsRoot 'gateway.stdout.log') -RedirectStandardError (Join-Path $LogsRoot 'gateway.stderr.log') -PassThru -WindowStyle Hidden",
            "[Console]::Out.WriteLine([string]$Process.Id)",
        ]
    )
    return _powershell_command(script)


def _posix_gateway_start_command(
    target: GatewaySshTarget,
    *,
    remote_workspace_path: str,
    remote_gateway_port: int,
    remote_boxteam_home: str,
) -> str:
    remote_config_root = _remote_join(target, remote_boxteam_home, "config")
    remote_logs_root = _remote_join(target, remote_boxteam_home, "logs")
    runtime_manifest_path = _remote_join(
        target,
        remote_boxteam_home,
        "runtime",
        "runtime-manifest.json",
    )
    gateway_root = _remote_join(
        target,
        remote_boxteam_home,
        "state",
        "gateway",
    )
    python = _remote_python(target)
    config_files = (
        ("gateway_inline.jsonc", "gateway.jsonc"),
        ("gateway_schema.jsonc", "gateway_schema.jsonc"),
        ("workspace_inline.jsonc", "workspace.jsonc"),
        ("workspace_schema.jsonc", "workspace_schema.jsonc"),
    )
    if target.platform == "linux":
        runtime_preparation: tuple[str, ...] = ()
        runtime_paths = (
            "BOXTEAM_NODE_BIN=/usr/local/bin/node",
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium",
        )
    else:
        # TODO: 用原生 macOS 目标验证 Node 与 Playwright Chromium 的安装位置。
        runtime_preparation = ("node_executable=$(command -v node);",)
        runtime_paths = ('BOXTEAM_NODE_BIN="$node_executable"',)
    commands = [
        "set -e;",
        f"rm -rf {shlex.quote(remote_boxteam_home)} {shlex.quote(remote_workspace_path)};",
        "mkdir -p "
        + " ".join(
            shlex.quote(path)
            for path in (remote_config_root, remote_logs_root, remote_workspace_path)
        )
        + ";",
        *(
            f"cp {shlex.quote(_remote_join(target, target.repository_path, 'configs', source))} "
            f"{shlex.quote(_remote_join(target, remote_config_root, destination))};"
            for source, destination in config_files
        ),
        f"cd {shlex.quote(target.repository_path)};",
        _posix_runtime_manifest_command(target, runtime_manifest_path) + ";",
        *runtime_preparation,
        f"BOXTEAM_RUNTIME_MANIFEST={shlex.quote(runtime_manifest_path)}",
        f"BOXTEAM_HOME={shlex.quote(remote_boxteam_home)}",
        f"BOXTEAM_GATEWAY_ROOT={shlex.quote(gateway_root)}",
        f"BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT={shlex.quote(remote_workspace_path)}",
        f"BOXTEAM_PYTHON_BIN={shlex.quote(python)}",
        *runtime_paths,
        "PYTHONUNBUFFERED=1",
        "nohup",
        shlex.quote(python),
        "-m",
        "uvicorn",
        "app.gateway.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(remote_gateway_port),
        "--log-level",
        "warning",
        ">",
        shlex.quote(_remote_join(target, remote_logs_root, "gateway.stdout.log")),
        "2>",
        shlex.quote(_remote_join(target, remote_logs_root, "gateway.stderr.log")),
        "<",
        "/dev/null",
        "&",
        "echo",
        "$!",
    ]
    return " ".join(commands)


def start_remote_gateway_via_ssh(
    *,
    target: GatewaySshTarget,
    remote_workspace_path: str,
    remote_gateway_port: int,
    remote_boxteam_home: str,
) -> str:
    """在远端启动 Gateway，并让它拥有默认工作区后端。"""
    if not 1 <= remote_gateway_port <= 65535:
        raise ValueError(f"Gateway 端口无效: {remote_gateway_port}")
    clear_remote_listeners(
        target,
        [remote_gateway_port, 8010, 8012, 8015],
    )
    if target.platform == "windows":
        command = _windows_gateway_start_command(
            target,
            remote_workspace_path=remote_workspace_path,
            remote_gateway_port=remote_gateway_port,
            remote_boxteam_home=remote_boxteam_home,
        )
    else:
        command = _posix_gateway_start_command(
            target,
            remote_workspace_path=remote_workspace_path,
            remote_gateway_port=remote_gateway_port,
            remote_boxteam_home=remote_boxteam_home,
        )
    process_id = run_ssh_command(target, command, timeout=20).stdout.strip()
    if not process_id.isdecimal():
        raise RuntimeError(f"远端 Gateway 启动命令没有返回有效 PID: {process_id!r}")
    wait_for_remote_gateway_ready(
        target=target,
        remote_gateway_port=remote_gateway_port,
        process_id=process_id,
        remote_boxteam_home=remote_boxteam_home,
    )
    return process_id


def wait_for_remote_gateway_ready(
    *,
    target: GatewaySshTarget,
    remote_gateway_port: int,
    process_id: str,
    remote_boxteam_home: str,
) -> None:
    if not process_id.isdecimal():
        raise ValueError(f"远端 Gateway PID 无效: {process_id!r}")
    if not 1 <= remote_gateway_port <= 65535:
        raise ValueError(f"Gateway 端口无效: {remote_gateway_port}")
    deadline = time.monotonic() + TARGET_READY_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        if target.platform == "windows":
            stdout_path = _remote_join(
                target,
                remote_boxteam_home,
                "logs",
                "gateway.stdout.log",
            )
            stderr_path = _remote_join(
                target,
                remote_boxteam_home,
                "logs",
                "gateway.stderr.log",
            )
            probe = "\n".join(
                [
                    "$ErrorActionPreference = 'Stop'",
                    "Set-StrictMode -Version Latest",
                    f"$Process = Get-Process -Id {process_id} -ErrorAction SilentlyContinue",
                    "if ($null -eq $Process) {",
                    f"  Get-Content -LiteralPath {_powershell_quote(stdout_path)} -Raw -ErrorAction SilentlyContinue",
                    f"  Get-Content -LiteralPath {_powershell_quote(stderr_path)} -Raw -ErrorAction SilentlyContinue",
                    "  exit 73",
                    "}",
                    f"try {{ Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri 'http://127.0.0.1:{remote_gateway_port}/api/gateway/health' | Out-Null; exit 0 }}",
                    "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }",
                ]
            )
            remote_command = _powershell_command(probe)
        else:
            remote_command = (
                f"process_state=$(ps -o stat= -p {shlex.quote(process_id)} 2>/dev/null); "
                'case "$process_state" in ""|Z*) '
                f"cat {shlex.quote(remote_boxteam_home + '/logs/gateway.stdout.log')} "
                f"{shlex.quote(remote_boxteam_home + '/logs/gateway.stderr.log')}; "
                "exit 73;; esac; "
                f"curl -fsS --max-time 5 http://127.0.0.1:{remote_gateway_port}"
                "/api/gateway/health"
            )
        result = subprocess.run(
            ssh_command(target, remote_command),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 73:
            raise RuntimeError(
                "容器内远程 Gateway 进程已退出: "
                f"pid={process_id}, port={remote_gateway_port}, "
                f"stdout={result.stdout.strip()}, stderr={result.stderr.strip()}"
            )
        if result.returncode == 0:
            return
        last_error = result.stderr.strip() or result.stdout.strip()
        time.sleep(1)
    raise TimeoutError(
        "容器内远程 Gateway 未就绪: "
        f"pid={process_id}, port={remote_gateway_port}, last_error={last_error}"
    )


def stop_remote_gateway(target: GatewaySshTarget, process_id: str) -> None:
    _stop_remote_processes(target, [process_id])


def run_checked_command(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "命令执行失败: "
            f"{' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def ssh_command(target: GatewaySshTarget, remote_command: str) -> list[str]:
    target.known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    return [
        "ssh",
        "-i",
        str(target.private_key),
        "-p",
        str(target.ssh_port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=2",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={target.known_hosts_path}",
        f"{target.username}@{target.host}",
        remote_command,
    ]


def run_ssh_command(
    target: GatewaySshTarget,
    remote_command: str,
    *,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return run_checked_command(ssh_command(target, remote_command), timeout=timeout)


def wait_for_ssh_ready(target: GatewaySshTarget) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ssh_command(target, "echo ready"),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "ready":
            return
        last_error = result.stderr.strip()
        time.sleep(1)
    raise TimeoutError(f"SSH 目标在 {READY_TIMEOUT_SECONDS} 秒内未就绪: {last_error}")


def _stop_remote_processes(
    target: GatewaySshTarget,
    process_ids: list[str],
) -> None:
    if not process_ids:
        return
    if target.platform == "windows":
        if any(not process_id.isdecimal() for process_id in process_ids):
            raise ValueError(f"Windows 进程 ID 无效: {process_ids}")
        script = "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "Set-StrictMode -Version Latest",
                "foreach ($TargetProcessId in @(" + ", ".join(process_ids) + ")) {",
                "  if ($null -eq (Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue)) { continue }",
                "  $TaskkillOutput = & taskkill.exe /PID $TargetProcessId /T /F 2>&1",
                "  if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 128) { throw ($TaskkillOutput -join [Environment]::NewLine) }",
                "  $Deadline = [DateTime]::UtcNow.AddSeconds(10)",
                "  while ($null -ne (Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $Deadline) { Start-Sleep -Milliseconds 50 }",
                '  if ($null -ne (Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue)) { throw "Windows 进程树未退出: $TargetProcessId" }',
                "}",
            ]
        )
        result = subprocess.run(
            ssh_command(target, _powershell_command(script)),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Windows 远端进程树清理失败: "
                f"pids={process_ids}, stdout={result.stdout.strip()}, "
                f"stderr={result.stderr.strip()}"
            )
        return
    if any(not process_id.isdecimal() for process_id in process_ids):
        raise ValueError(f"POSIX 进程 ID 无效: {process_ids}")
    quoted_ids = " ".join(shlex.quote(process_id) for process_id in process_ids)
    command = (
        "set -eu; "
        "command -v ps >/dev/null; "
        'process_is_running() { state=$(ps -o stat= -p "$1" 2>/dev/null) || return 1; '
        'case "$state" in ""|Z*) return 1;; *) return 0;; esac; }; '
        f"for target_pid in {quoted_ids}; do "
        'if process_is_running "$target_pid"; then '
        'if ! kill "$target_pid" 2>/dev/null && process_is_running "$target_pid"; then '
        'echo "远端进程无法停止: $target_pid" >&2; exit 1; fi; '
        "attempt=0; "
        'while process_is_running "$target_pid" && [ "$attempt" -lt 100 ]; do '
        "sleep 0.05; attempt=$((attempt + 1)); done; "
        'if process_is_running "$target_pid"; then kill -KILL "$target_pid"; fi; '
        "sleep 0.05; "
        'if process_is_running "$target_pid"; then '
        'echo "远端进程仍在运行: $target_pid" >&2; exit 1; fi; '
        "fi; done"
    )
    result = subprocess.run(
        ssh_command(target, command),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "POSIX 远端进程清理失败: "
            f"pids={process_ids}, stdout={result.stdout.strip()}, "
            f"stderr={result.stderr.strip()}"
        )
