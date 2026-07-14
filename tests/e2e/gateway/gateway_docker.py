from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


READY_TIMEOUT_SECONDS = 60
DOCKER_READY_TIMEOUT_SECONDS = 240
SSH_KNOWN_HOSTS_PATH = Path("out/tests/e2e/gateway_ssh_known_hosts").resolve()
GATEWAY_TEST_IMAGE = "boxteam-gateway-ssh-target:local"


@dataclass(frozen=True, slots=True)
class GatewaySshDockerTarget:
    ssh_port: int
    username: str
    compose_project: str
    compose_env: dict[str, str]
    private_key: Path


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


def docker_image_exists(image: str) -> bool:
    result = subprocess.run(
        [*docker_command_prefix(), "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return result.returncode == 0


def start_gateway_ssh_container(*, ssh_port: int) -> GatewaySshDockerTarget:
    project_root = Path.cwd().resolve()
    private_key = project_root / "asset" / "gateway_ssh" / "id_ed25519"
    private_key.chmod(0o600)
    username = os.environ.get("BOXTEAM_GATEWAY_E2E_SSH_USER", "root").strip()
    if not username:
        raise ValueError("BOXTEAM_GATEWAY_E2E_SSH_USER 不能为空")
    compose_project = f"boxteam-gateway-e2e-{ssh_port}"
    compose_env = os.environ.copy()
    compose_env["BOXTEAM_GATEWAY_E2E_SSH_PORT"] = str(ssh_port)
    compose_env["BOXTEAM_GATEWAY_E2E_SSH_USER"] = username
    target = GatewaySshDockerTarget(
        ssh_port=ssh_port,
        username=username,
        compose_project=compose_project,
        compose_env=compose_env,
        private_key=private_key,
    )
    compose_up_args = ["up", "-d"]
    if (
        os.environ.get("BOXTEAM_GATEWAY_E2E_REBUILD_IMAGE") == "1"
        or not docker_image_exists(GATEWAY_TEST_IMAGE)
    ):
        compose_up_args.append("--build")
    else:
        compose_up_args.append("--no-build")
    run_checked_command(
        docker_compose_command(compose_project, *compose_up_args),
        env=compose_env,
        timeout=300,
    )
    wait_for_ssh_ready(target)
    return target


def stop_gateway_ssh_container(target: GatewaySshDockerTarget) -> None:
    subprocess.run(
        docker_compose_command(target.compose_project, "down", "-v", "--remove-orphans"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=target.compose_env,
        timeout=120,
        check=False,
    )


def start_remote_backend_via_ssh(
    *,
    target: GatewaySshDockerTarget,
    remote_workspace_path: str,
    remote_backend_port: int,
    extra_env: dict[str, str] | None = None,
) -> str:
    project_path = "/workspace/vscode-graph-agent-opencode"
    log_dir = f"{remote_workspace_path}/.boxteam/logs"
    env_parts = [
        f"{key}={shlex.quote(value)}"
        for key, value in (extra_env or {}).items()
    ]
    command = " ".join(
        [
            "set -e;",
            f"mkdir -p {shlex.quote(log_dir)};",
            f"cd {shlex.quote(project_path)};",
            f"WORKSPACE_ROOT={shlex.quote(remote_workspace_path)}",
            f"BOXTEAM_PROJECT_ROOT={shlex.quote(project_path)}",
            *env_parts,
            "PYTHONUNBUFFERED=1",
            "UV_PROJECT_ENVIRONMENT=/tmp/boxteam-gateway-e2e-venv",
            "nohup",
            "uv",
            "run",
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
    process_id = run_ssh_command(target, command, timeout=10).stdout.strip()
    if not process_id:
        raise RuntimeError("容器内后端启动命令没有返回 pid")
    return process_id


def container_host_gateway(target: GatewaySshDockerTarget) -> str:
    command = (
        "python - <<'PY'\n"
        "import socket, struct\n"
        "with open('/proc/net/route', encoding='utf-8') as handle:\n"
        "    for line in handle.readlines()[1:]:\n"
        "        fields = line.split()\n"
        "        if len(fields) >= 3 and fields[1] == '00000000':\n"
        "            print(socket.inet_ntoa(struct.pack('<L', int(fields[2], 16))))\n"
        "            break\n"
        "    else:\n"
        "        raise SystemExit('default gateway not found')\n"
        "PY"
    )
    gateway = run_ssh_command(target, command, timeout=10).stdout.strip()
    if not gateway:
        raise RuntimeError("无法解析容器访问宿主机的 gateway IP")
    return gateway


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
    subprocess.run(
        ssh_command(target, f"kill {shlex.quote(process_id)} || true"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )


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


def docker_compose_command(project_name: str, *args: str) -> list[str]:
    compose_file = Path.cwd().resolve() / "tools" / "docker-compose.gateway-test.yml"
    return [
        *docker_command_prefix(),
        "compose",
        "-f",
        str(compose_file),
        "-p",
        project_name,
        *args,
    ]


def ssh_command(target: GatewaySshDockerTarget, remote_command: str) -> list[str]:
    SSH_KNOWN_HOSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
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
        f"UserKnownHostsFile={SSH_KNOWN_HOSTS_PATH}",
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
