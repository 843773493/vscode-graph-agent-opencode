from __future__ import annotations

import asyncio
import os
import random
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.gateway.service_types import LocalForwardSpec
from app.gateway.ssh_command import build_ssh_command


GATEWAY_PROCESS_READY_TIMEOUT_SECONDS = 45
DEFAULT_SSH_TUNNEL_PORT_MIN = 41000
DEFAULT_SSH_TUNNEL_PORT_MAX = 41999


@dataclass(slots=True)
class ManagedProcess:
    process: subprocess.Popen[str]
    log_file: object

    def close(self, *, timeout_seconds: float = 10) -> None:
        try:
            if self.process.poll() is None:
                self._terminate_group()
                try:
                    self.process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._kill_group()
                    self.process.wait(timeout=timeout_seconds)
        finally:
            close = getattr(self.log_file, "close", None)
            if callable(close):
                close()

    def _terminate_group(self) -> None:
        if os.name == "posix":
            os.killpg(self.process.pid, signal.SIGTERM)
        else:
            self.process.terminate()

    def _kill_group(self) -> None:
        if os.name == "posix":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        else:
            self.process.kill()


def allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _bindable_local_port(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _parse_port_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} 必须是整数端口号: {raw_value}") from error
    if value < 1 or value > 65535:
        raise ValueError(f"{name} 必须是 1-65535 的端口号: {raw_value}")
    return value


def ssh_tunnel_port_range_from_env() -> tuple[int, int]:
    start = _parse_port_env(
        "BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MIN",
        DEFAULT_SSH_TUNNEL_PORT_MIN,
    )
    end = _parse_port_env(
        "BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MAX",
        DEFAULT_SSH_TUNNEL_PORT_MAX,
    )
    if start > end:
        raise ValueError(
            "BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MIN 不能大于 "
            f"BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MAX: {start}>{end}"
        )
    return start, end


def allocate_local_port_in_range(start: int, end: int) -> int:
    if start < 1 or end > 65535 or start > end:
        raise ValueError(f"端口范围无效: {start}-{end}")
    ports = list(range(start, end + 1))
    random.SystemRandom().shuffle(ports)
    for port in ports:
        if _bindable_local_port(port):
            return port
    raise RuntimeError(f"端口范围内没有可用本地端口: {start}-{end}")


def allocate_ssh_tunnel_port() -> int:
    start, end = ssh_tunnel_port_range_from_env()
    return allocate_local_port_in_range(start, end)


def resolve_python_executable(_: Path) -> Path:
    configured = os.environ.get("BOXTEAM_PYTHON_BIN")
    if configured:
        executable = Path(configured).expanduser()
        if not executable.is_absolute():
            raise ValueError(
                f"BOXTEAM_PYTHON_BIN 必须是绝对路径: {configured}"
            )
        # 不解引用 venv 的 python 符号链接；真实路径会丢失 pyvenv.cfg 上下文。
        return executable
    return Path(sys.executable).absolute()


async def wait_for_http_ok(url: str, process: subprocess.Popen[str] | None = None) -> None:
    deadline = asyncio.get_running_loop().time() + GATEWAY_PROCESS_READY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=2) as client:
        while asyncio.get_running_loop().time() < deadline:
            if process is not None and process.poll() is not None:
                raise RuntimeError(
                    f"进程提前退出: pid={process.pid}, returncode={process.returncode}, url={url}"
                )
            try:
                response = await client.get(url, headers={"X-Local-Token": "local-dev-token"})
                if response.status_code == 200:
                    return
                last_error = RuntimeError(
                    f"健康检查返回 {response.status_code}: {response.text[:300]}"
                )
            except Exception as error:
                last_error = error
            await asyncio.sleep(0.5)

    detail = f"，最后错误: {last_error}" if last_error else ""
    raise TimeoutError(f"目标服务在 {GATEWAY_PROCESS_READY_TIMEOUT_SECONDS} 秒内未就绪: {url}{detail}")


def start_local_backend_process(
    *,
    project_root: Path,
    workspace_root: Path,
    port: int,
    log_dir: Path,
    extra_env: dict[str, str] | None = None,
    debug_port: int | None = None,
) -> ManagedProcess:
    python_executable = resolve_python_executable(project_root)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / f"local-backend-{port}.log", "a", encoding="utf-8")
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    env["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        env.update(extra_env)
    command = [str(python_executable)]
    if debug_port is not None:
        command.extend(
            [
                "-m",
                "debugpy",
                "--listen",
                f"127.0.0.1:{debug_port}",
            ]
        )
    command.extend(
        [
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
    )
    process = subprocess.Popen(
        command,
        cwd=project_root,
        env=env,
        stdout=log_file,
        stderr=log_file,
        text=True,
        start_new_session=os.name == "posix",
    )
    return ManagedProcess(process=process, log_file=log_file)


def start_local_node_service_process(
    *,
    project_root: Path,
    workspace_root: Path,
    service: str,
    port: int,
    log_dir: Path,
) -> ManagedProcess:
    if service not in {"terminal", "browser"}:
        raise ValueError(f"不支持的本地辅助服务: {service}")
    backend_path = project_root / "src" / service / "server" / "backend.js"
    if not backend_path.is_file():
        raise FileNotFoundError(f"辅助服务入口不存在: {backend_path}")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / f"local-{service}-{port}.log", "a", encoding="utf-8")
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    node_executable = os.environ.get("BOXTEAM_NODE_BIN")
    if not node_executable:
        raise RuntimeError("启动本地辅助服务必须通过 BOXTEAM_NODE_BIN 显式提供 Node")
    process = subprocess.Popen(
        [
            node_executable,
            str(backend_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workspace-root",
            str(workspace_root),
            "--frontend-url",
            os.environ.get(
                (
                    "BOXTEAM_TERMINAL_FRONTEND_URL"
                    if service == "terminal"
                    else "BOXTEAM_BROWSER_FRONTEND_URL"
                ),
                "http://127.0.0.1",
            ),
        ],
        cwd=project_root,
        env=env,
        stdout=log_file,
        stderr=log_file,
        text=True,
        start_new_session=os.name == "posix",
    )
    return ManagedProcess(process=process, log_file=log_file)


def start_ssh_tunnel_process(
    *,
    host: str,
    port: int,
    username: str,
    private_key_path: Path | None,
    ssh_config_host: str | None,
    forwards: tuple[LocalForwardSpec, ...],
    log_dir: Path,
) -> ManagedProcess:
    if not forwards:
        raise ValueError("SSH 隧道至少需要一个端口转发")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(
        log_dir / f"ssh-tunnel-{forwards[0].local_port}.log",
        "a",
        encoding="utf-8",
    )
    forward_arguments: list[str] = ["-N"]
    for forward in forwards:
        forward_arguments.extend(
            [
                "-L",
                (
                    f"127.0.0.1:{forward.local_port}:"
                    f"{forward.remote_host}:{forward.remote_port}"
                ),
            ]
        )
    forward_arguments.extend(["-o", "ExitOnForwardFailure=yes"])
    process = subprocess.Popen(
        build_ssh_command(
            host=host,
            port=port,
            username=username,
            private_key_path=str(private_key_path) if private_key_path is not None else None,
            ssh_config_host=ssh_config_host,
            extra_arguments=forward_arguments,
        ),
        stdout=log_file,
        stderr=log_file,
        text=True,
        start_new_session=os.name == "posix",
    )
    return ManagedProcess(process=process, log_file=log_file)
