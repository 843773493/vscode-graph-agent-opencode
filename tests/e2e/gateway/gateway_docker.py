from __future__ import annotations

import fcntl
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from configs.boxteam import SSH_KEY_NAME, install_development_ssh_assets


READY_TIMEOUT_SECONDS = 60
DOCKER_READY_TIMEOUT_SECONDS = 240
GATEWAY_TEST_IMAGE = "boxteam-gateway-ssh-target:services-v4"
GATEWAY_COMPOSE_PROJECT = "boxteam-gateway-target"
GATEWAY_COMPOSE_SERVICE = "boxteam-gateway-ssh-target"
CONTAINER_PROJECT_PATH = "/workspace/vscode-graph-agent-opencode"


@dataclass(frozen=True, slots=True)
class GatewaySshDockerTarget:
    ssh_port: int
    username: str
    compose_env: dict[str, str]
    private_key: Path
    known_hosts_path: Path
    container_id: str
    python_environment: str


@dataclass(frozen=True, slots=True)
class RemoteAuxiliaryProcesses:
    terminal_pid: str
    terminal_port: int
    browser_pid: str
    browser_port: int


def docker_daemon_error() -> str | None:
    result = subprocess.run(
        [*docker_command_prefix(), "version", "--format", "{{.Server.Version}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    return result.stderr.strip()


def ensure_gateway_ssh_container(*, known_hosts_path: Path) -> GatewaySshDockerTarget:
    project_root = Path.cwd().resolve()
    private_key = (
        project_root / "asset" / "gateway_ssh" / "boxteam_gateway_e2e_ed25519"
    )
    private_key.chmod(0o600)
    username = os.environ.get("BOXTEAM_GATEWAY_E2E_SSH_USER", "root").strip()
    if not username:
        raise ValueError("BOXTEAM_GATEWAY_E2E_SSH_USER 不能为空")
    ssh_port = int(os.environ.get("BOXTEAM_GATEWAY_E2E_SSH_PORT", "22222"))
    compose_env = _compose_environment(ssh_port=ssh_port, username=username)

    lock_path = project_root / "out" / "docker" / ".gateway-target.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        compose_up_args = ["up", "-d"]
        if (
            os.environ.get("BOXTEAM_GATEWAY_E2E_REBUILD_IMAGE") == "1"
            or not docker_image_exists(GATEWAY_TEST_IMAGE)
        ):
            compose_up_args.append("--build")
        else:
            compose_up_args.append("--no-build")
        run_checked_command(
            docker_compose_command(*compose_up_args),
            env=compose_env,
            timeout=900,
        )
        container_id = _compose_container_id(compose_env)
        python_environment = _ensure_container_python_environment(container_id)

    target = GatewaySshDockerTarget(
        ssh_port=ssh_port,
        username=username,
        compose_env=compose_env,
        private_key=private_key,
        known_hosts_path=known_hosts_path.resolve(),
        container_id=container_id,
        python_environment=python_environment,
    )
    wait_for_ssh_ready(target)
    return target


def install_gateway_ssh_assets_for_e2e(workspace_root: Path) -> Path:
    project_root = Path.cwd().resolve()
    test_home = workspace_root.parent / "artifacts" / "home"
    install_development_ssh_assets(project_root=project_root, home=test_home)
    return test_home / ".ssh" / SSH_KEY_NAME


def clear_remote_listeners(
    target: GatewaySshDockerTarget,
    ports: list[int],
) -> None:
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
    target: GatewaySshDockerTarget,
    remote_workspace_path: str,
    remote_backend_port: int,
    extra_env: dict[str, str] | None = None,
) -> str:
    clear_remote_listeners(target, [remote_backend_port])
    log_dir = f"{remote_workspace_path}/.boxteam/logs"
    remote_config_path = f"{remote_workspace_path}/.boxteam/boxteam.jsonc"
    remote_schema_path = f"{remote_workspace_path}/.boxteam/config.schema.jsonc"
    test_config_source = f"{CONTAINER_PROJECT_PATH}/configs/tests/default.jsonc"
    config_schema_source = f"{CONTAINER_PROJECT_PATH}/configs/config.jsonc"
    env_parts = [
        f"{key}={shlex.quote(value)}" for key, value in (extra_env or {}).items()
    ]
    command = " ".join(
        [
            "set -e;",
            f"mkdir -p {shlex.quote(log_dir)};",
            f"cp {shlex.quote(test_config_source)} {shlex.quote(remote_config_path)};",
            f"cp {shlex.quote(config_schema_source)} {shlex.quote(remote_schema_path)};",
            f"cd {shlex.quote(CONTAINER_PROJECT_PATH)};",
            f"WORKSPACE_ROOT={shlex.quote(remote_workspace_path)}",
            f"BOXTEAM_PROJECT_ROOT={shlex.quote(CONTAINER_PROJECT_PATH)}",
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


def start_remote_auxiliary_services_via_ssh(
    *,
    target: GatewaySshDockerTarget,
    remote_workspace_path: str,
    terminal_port: int,
    browser_port: int,
) -> RemoteAuxiliaryProcesses:
    clear_remote_listeners(target, [terminal_port, browser_port])
    log_dir = f"{remote_workspace_path}/.boxteam/logs"
    command = " ".join(
        [
            "set -e;",
            f"mkdir -p {shlex.quote(log_dir)};",
            f"cd {shlex.quote(CONTAINER_PROJECT_PATH)};",
            f"WORKSPACE_ROOT={shlex.quote(remote_workspace_path)}",
            f"BOXTEAM_PROJECT_ROOT={shlex.quote(CONTAINER_PROJECT_PATH)}",
            "nohup node src/terminal/server/backend.js",
            f"--host 127.0.0.1 --port {terminal_port}",
            f"--workspace-root {shlex.quote(remote_workspace_path)}",
            f"> {shlex.quote(log_dir)}/remote-terminal.stdout.log",
            f"2> {shlex.quote(log_dir)}/remote-terminal.stderr.log < /dev/null &",
            "terminal_pid=$!;",
            f"WORKSPACE_ROOT={shlex.quote(remote_workspace_path)}",
            f"BOXTEAM_PROJECT_ROOT={shlex.quote(CONTAINER_PROJECT_PATH)}",
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
    target: GatewaySshDockerTarget,
    processes: RemoteAuxiliaryProcesses,
) -> None:
    deadline = time.monotonic() + DOCKER_READY_TIMEOUT_SECONDS
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
    target: GatewaySshDockerTarget,
    processes: RemoteAuxiliaryProcesses,
) -> None:
    _stop_remote_processes(
        target,
        [processes.terminal_pid, processes.browser_pid],
    )


def wait_for_remote_backend_ready(
    *,
    target: GatewaySshDockerTarget,
    remote_backend_port: int,
    remote_backend_pid: str,
) -> None:
    deadline = time.monotonic() + DOCKER_READY_TIMEOUT_SECONDS
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
        f"{DOCKER_READY_TIMEOUT_SECONDS} 秒内未就绪: pid={remote_backend_pid}, "
        f"port={remote_backend_port}, last_error={last_error}"
    )


def stop_remote_backend(target: GatewaySshDockerTarget, process_id: str) -> None:
    _stop_remote_processes(target, [process_id])


def docker_image_exists(image: str) -> bool:
    result = subprocess.run(
        [*docker_command_prefix(), "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


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


def docker_command_prefix() -> list[str]:
    raw_command = os.environ.get("BOXTEAM_DOCKER_COMMAND", "docker").strip()
    if not raw_command:
        raise ValueError("BOXTEAM_DOCKER_COMMAND 不能为空")
    return shlex.split(raw_command)


def docker_compose_command(*args: str) -> list[str]:
    compose_file = Path.cwd().resolve() / "tools" / "docker-compose.gateway-test.yml"
    return [
        *docker_command_prefix(),
        "compose",
        "-f",
        str(compose_file),
        "-p",
        GATEWAY_COMPOSE_PROJECT,
        *args,
    ]


def ssh_command(target: GatewaySshDockerTarget, remote_command: str) -> list[str]:
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
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={target.known_hosts_path}",
        f"{target.username}@127.0.0.1",
        remote_command,
    ]


def run_ssh_command(
    target: GatewaySshDockerTarget,
    remote_command: str,
    *,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return run_checked_command(ssh_command(target, remote_command), timeout=timeout)


def wait_for_ssh_ready(target: GatewaySshDockerTarget) -> None:
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
    raise TimeoutError(f"SSH 容器在 {READY_TIMEOUT_SECONDS} 秒内未就绪: {last_error}")


def _compose_environment(*, ssh_port: int, username: str) -> dict[str, str]:
    compose_env = os.environ.copy()
    compose_env["BOXTEAM_GATEWAY_E2E_SSH_PORT"] = str(ssh_port)
    compose_env["BOXTEAM_GATEWAY_E2E_SSH_USER"] = username
    playwright_cache = Path(
        os.environ.get(
            "BOXTEAM_GATEWAY_E2E_PLAYWRIGHT_CACHE",
            str(Path.home() / ".cache" / "ms-playwright"),
        )
    ).expanduser().resolve()
    if not playwright_cache.is_dir():
        raise FileNotFoundError(
            f"Gateway Browser E2E 缺少 Playwright 浏览器缓存: {playwright_cache}"
        )
    compose_env["BOXTEAM_GATEWAY_E2E_PLAYWRIGHT_CACHE"] = str(playwright_cache)
    return compose_env


def _compose_container_id(compose_env: dict[str, str]) -> str:
    result = run_checked_command(
        docker_compose_command("ps", "-q", GATEWAY_COMPOSE_SERVICE),
        env=compose_env,
    )
    container_id = result.stdout.strip()
    if not container_id:
        raise RuntimeError("Compose 未返回 Gateway 测试目标容器 ID")
    return container_id


def _ensure_container_python_environment(container_id: str) -> str:
    os_result = run_checked_command(
        [
            *docker_command_prefix(),
            "exec",
            container_id,
            "sh",
            "-lc",
            '. /etc/os-release; printf "%s%s" "$ID" "$VERSION_ID"',
        ]
    )
    os_id = re.sub(r"[^a-z0-9]", "", os_result.stdout.strip().lower())
    if not os_id:
        raise RuntimeError(f"无法解析容器系统简称: {os_result.stdout!r}")
    environment = f"{CONTAINER_PROJECT_PATH}/.venv_docker_{os_id}"
    run_checked_command(
        [
            *docker_command_prefix(),
            "exec",
            container_id,
            "sh",
            "-lc",
            (
                f"cd {shlex.quote(CONTAINER_PROJECT_PATH)}; "
                f"UV_PROJECT_ENVIRONMENT={shlex.quote(environment)} uv sync --frozen"
            ),
        ],
        timeout=900,
    )
    return environment


def _stop_remote_processes(
    target: GatewaySshDockerTarget,
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
