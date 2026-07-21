from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from configs.boxteam import SSH_KEY_NAME, install_development_ssh_assets


READY_TIMEOUT_SECONDS = 60
TARGET_READY_TIMEOUT_SECONDS = 240


@dataclass(frozen=True, slots=True)
class GatewaySshTarget:
    target_id: str
    platform: Literal["linux", "windows"]
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


@dataclass(frozen=True, slots=True)
class RemoteAuxiliaryProcesses:
    terminal_pid: str
    terminal_port: int
    browser_pid: str
    browser_port: int


def build_remote_pair_command(
    target: GatewaySshTarget,
    *,
    remote_boxteam_home: str,
) -> str:
    if target.platform == "windows":
        # TODO: 获得 VMware Windows 资源后执行真实联邦配对 E2E。
        python = f"{target.python_environment}\\Scripts\\python.exe"
        return (
            f'powershell -NoProfile -Command "Set-Location \'{target.repository_path}\'; '
            f"$env:BOXTEAM_HOME='{remote_boxteam_home}'; "
            f"$env:BOXTEAM_GATEWAY_ROOT='{remote_boxteam_home}\\state\\gateway'; "
            f"& '{python}' -m app.gateway.federation_pairing\""
        )
    return (
        f"cd {shlex.quote(target.repository_path)} && "
        f"BOXTEAM_HOME={shlex.quote(remote_boxteam_home)} "
        f"BOXTEAM_GATEWAY_ROOT={shlex.quote(remote_boxteam_home + '/state/gateway')} "
        f"{shlex.quote(target.python_environment + '/bin/python')} "
        "-m app.gateway.federation_pairing"
    )


def _require_linux_target(target: GatewaySshTarget, operation: str) -> None:
    if target.platform != "linux":
        # TODO: 获得 VMware Windows 资源后补齐进程生命周期与端口清理实现。
        raise NotImplementedError(f"{operation} 尚未在 Windows 目标上完成真实验证")


def install_gateway_ssh_assets_for_e2e(workspace_root: Path) -> Path:
    project_root = Path.cwd().resolve()
    test_home = workspace_root.parent / "artifacts" / "home"
    install_development_ssh_assets(project_root=project_root, home=test_home)
    return test_home / ".ssh" / SSH_KEY_NAME


def clear_remote_listeners(
    target: GatewaySshTarget,
    ports: list[int],
) -> None:
    _require_linux_target(target, "清理远端监听端口")
    normalized_ports = sorted(set(ports))
    if not normalized_ports:
        return
    port_values = " ".join(str(port) for port in normalized_ports)
    run_ssh_command(
        target,
        (
            f"for port in {port_values}; do "
            'pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN || true); '
            'if [ -n "$pids" ]; then kill $pids; fi; '
            "done"
        ),
        timeout=20,
    )


def start_remote_backend_via_ssh(
    *,
    target: GatewaySshTarget,
    remote_workspace_path: str,
    remote_backend_port: int,
    extra_env: dict[str, str] | None = None,
) -> str:
    _require_linux_target(target, "启动远端工作区后端")
    clear_remote_listeners(target, [remote_backend_port])
    log_dir = f"{remote_workspace_path}/.boxteam/logs"
    remote_config_path = f"{remote_workspace_path}/.boxteam/boxteam.jsonc"
    remote_schema_path = f"{remote_workspace_path}/.boxteam/config.schema.jsonc"
    test_config_source = f"{target.repository_path}/configs/tests/default.jsonc"
    config_schema_source = f"{target.repository_path}/configs/config.jsonc"
    env_parts = [
        f"{key}={shlex.quote(value)}" for key, value in (extra_env or {}).items()
    ]
    command = " ".join(
        [
            "set -e;",
            f"mkdir -p {shlex.quote(log_dir)};",
            f"cp {shlex.quote(test_config_source)} {shlex.quote(remote_config_path)};",
            f"cp {shlex.quote(config_schema_source)} {shlex.quote(remote_schema_path)};",
            f"cd {shlex.quote(target.repository_path)};",
            f"WORKSPACE_ROOT={shlex.quote(remote_workspace_path)}",
            f"BOXTEAM_PROJECT_ROOT={shlex.quote(target.repository_path)}",
            f"BOXTEAM_USER_CONFIG_PATH={shlex.quote(remote_config_path)}",
            *env_parts,
            "PYTHONUNBUFFERED=1",
            f"UV_PROJECT_ENVIRONMENT={shlex.quote(target.python_environment)}",
            "nohup",
            shlex.quote(f"{target.python_environment}/bin/python"),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(remote_backend_port),
            "--log-level",
            "warning",
            ">",
            f"{shlex.quote(log_dir)}/remote-backend.stdout.log",
            "2>",
            f"{shlex.quote(log_dir)}/remote-backend.stderr.log",
            "<",
            "/dev/null",
            "&",
            "echo",
            "$!",
        ]
    )
    process_id = run_ssh_command(target, command, timeout=15).stdout.strip()
    if not process_id:
        raise RuntimeError("容器内后端启动命令没有返回 pid")
    return process_id


def start_remote_gateway_via_ssh(
    *,
    target: GatewaySshTarget,
    remote_workspace_path: str,
    remote_gateway_port: int,
    remote_boxteam_home: str,
) -> str:
    """在 SSH 容器内启动完整 Gateway，由它拥有远端默认工作区后端。"""
    _require_linux_target(target, "启动远端 Gateway")
    clear_remote_listeners(
        target,
        [
            remote_gateway_port,
            8010,
            8012,
            8015,
        ],
    )
    remote_config_root = f"{remote_boxteam_home}/config"
    remote_config_path = f"{remote_config_root}/boxteam.jsonc"
    remote_schema_path = f"{remote_config_root}/config.schema.jsonc"
    command = " ".join(
        [
            "set -e;",
            f"rm -rf {shlex.quote(remote_boxteam_home)}",
            f"{shlex.quote(remote_workspace_path)};",
            f"mkdir -p {shlex.quote(remote_config_root)}",
            f"{shlex.quote(remote_boxteam_home)}/logs",
            f"{shlex.quote(remote_workspace_path)};",
            f"cp {target.repository_path}/configs/tests/default.jsonc",
            f"{shlex.quote(remote_config_path)};",
            f"cp {target.repository_path}/configs/config.jsonc",
            f"{shlex.quote(remote_schema_path)};",
            f"cd {shlex.quote(target.repository_path)};",
            f"BOXTEAM_HOME={shlex.quote(remote_boxteam_home)}",
            f"BOXTEAM_GATEWAY_ROOT={shlex.quote(remote_boxteam_home + '/state/gateway')}",
            f"BOXTEAM_USER_CONFIG_PATH={shlex.quote(remote_config_path)}",
            f"BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT={shlex.quote(remote_workspace_path)}",
            f"BOXTEAM_PYTHON_BIN={shlex.quote(target.python_environment + '/bin/python')}",
            "BOXTEAM_NODE_BIN=/usr/local/bin/node",
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium",
            "PYTHONUNBUFFERED=1",
            "nohup",
            shlex.quote(f"{target.python_environment}/bin/python"),
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
            shlex.quote(remote_boxteam_home + "/logs/gateway.stdout.log"),
            "2>",
            shlex.quote(remote_boxteam_home + "/logs/gateway.stderr.log"),
            "<",
            "/dev/null",
            "&",
            "echo",
            "$!",
        ]
    )
    process_id = run_ssh_command(target, command, timeout=20).stdout.strip()
    if not process_id:
        raise RuntimeError("容器内远程 Gateway 启动命令没有返回 pid")
    wait_for_remote_gateway_ready(
        target=target,
        remote_gateway_port=remote_gateway_port,
        process_id=process_id,
    )
    return process_id


def wait_for_remote_gateway_ready(
    *,
    target: GatewaySshTarget,
    remote_gateway_port: int,
    process_id: str,
) -> None:
    deadline = time.monotonic() + TARGET_READY_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ssh_command(
                target,
                (
                    f"kill -0 {shlex.quote(process_id)} "
                    f"&& curl -fsS http://127.0.0.1:{remote_gateway_port}"
                    "/api/gateway/health"
                ),
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        last_error = result.stderr.strip() or result.stdout.strip()
        time.sleep(1)
    raise TimeoutError(
        "容器内远程 Gateway 未就绪: "
        f"pid={process_id}, port={remote_gateway_port}, last_error={last_error}"
    )


def start_remote_auxiliary_services_via_ssh(
    *,
    target: GatewaySshTarget,
    remote_workspace_path: str,
    terminal_port: int,
    browser_port: int,
) -> RemoteAuxiliaryProcesses:
    _require_linux_target(target, "启动远端辅助服务")
    clear_remote_listeners(target, [terminal_port, browser_port])
    log_dir = f"{remote_workspace_path}/.boxteam/logs"
    command = " ".join(
        [
            "set -e;",
            f"mkdir -p {shlex.quote(log_dir)};",
            f"cd {shlex.quote(target.repository_path)};",
            f"WORKSPACE_ROOT={shlex.quote(remote_workspace_path)}",
            f"BOXTEAM_PROJECT_ROOT={shlex.quote(target.repository_path)}",
            "nohup node src/terminal/server/backend.js",
            f"--host 127.0.0.1 --port {terminal_port}",
            f"--workspace-root {shlex.quote(remote_workspace_path)}",
            f"> {shlex.quote(log_dir)}/remote-terminal.stdout.log",
            f"2> {shlex.quote(log_dir)}/remote-terminal.stderr.log < /dev/null &",
            "terminal_pid=$!;",
            f"WORKSPACE_ROOT={shlex.quote(remote_workspace_path)}",
            f"BOXTEAM_PROJECT_ROOT={shlex.quote(target.repository_path)}",
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium",
            "nohup node src/browser/server/backend.js",
            f"--host 127.0.0.1 --port {browser_port}",
            f"--workspace-root {shlex.quote(remote_workspace_path)}",
            f"> {shlex.quote(log_dir)}/remote-browser.stdout.log",
            f"2> {shlex.quote(log_dir)}/remote-browser.stderr.log < /dev/null &",
            "browser_pid=$!;",
            'printf "%s %s\\n" "$terminal_pid" "$browser_pid"',
        ]
    )
    process_ids = run_ssh_command(target, command, timeout=15).stdout.strip().split()
    if len(process_ids) != 2:
        raise RuntimeError(f"容器内辅助服务启动命令返回异常: {process_ids}")
    processes = RemoteAuxiliaryProcesses(
        terminal_pid=process_ids[0],
        terminal_port=terminal_port,
        browser_pid=process_ids[1],
        browser_port=browser_port,
    )
    wait_for_remote_auxiliary_services(target=target, processes=processes)
    return processes


def wait_for_remote_auxiliary_services(
    *,
    target: GatewaySshTarget,
    processes: RemoteAuxiliaryProcesses,
) -> None:
    deadline = time.monotonic() + TARGET_READY_TIMEOUT_SECONDS
    last_error = ""
    command = (
        f"kill -0 {shlex.quote(processes.terminal_pid)} "
        f"&& kill -0 {shlex.quote(processes.browser_pid)} "
        f"&& curl -fsS http://127.0.0.1:{processes.terminal_port}/health "
        f"&& curl -fsS http://127.0.0.1:{processes.browser_port}/health"
    )
    while time.monotonic() < deadline:
        result = subprocess.run(
            ssh_command(target, command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode == 0:
            return
        last_error = result.stderr.strip()
        time.sleep(2)
    raise TimeoutError(
        "容器内辅助服务未就绪: "
        f"terminal_pid={processes.terminal_pid}, browser_pid={processes.browser_pid}, "
        f"last_error={last_error}"
    )


def stop_remote_auxiliary_services(
    target: GatewaySshTarget,
    processes: RemoteAuxiliaryProcesses,
) -> None:
    _stop_remote_processes(
        target,
        [processes.terminal_pid, processes.browser_pid],
    )


def wait_for_remote_backend_ready(
    *,
    target: GatewaySshTarget,
    remote_backend_port: int,
    remote_backend_pid: str,
) -> None:
    deadline = time.monotonic() + TARGET_READY_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ssh_command(
                target,
                (
                    f"kill -0 {shlex.quote(remote_backend_pid)} "
                    f"&& curl -fsS http://127.0.0.1:{remote_backend_port}/api/v1/health"
                ),
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return
        last_error = result.stderr.strip()
        time.sleep(2)
    raise TimeoutError(
        "容器内后端在 "
        f"{TARGET_READY_TIMEOUT_SECONDS} 秒内未就绪: pid={remote_backend_pid}, "
        f"port={remote_backend_port}, last_error={last_error}"
    )


def stop_remote_backend(target: GatewaySshTarget, process_id: str) -> None:
    _stop_remote_processes(target, [process_id])


def run_checked_command(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
    quoted_ids = " ".join(shlex.quote(process_id) for process_id in process_ids)
    subprocess.run(
        ssh_command(target, f"kill {quoted_ids} || true"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
