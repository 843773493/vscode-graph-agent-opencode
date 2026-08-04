from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

import httpx

from app.gateway.runtime.process_logs import ProcessLogStore
from app.gateway.service_types import LocalForwardSpec
from app.gateway.ssh_command import build_ssh_command

GATEWAY_PROCESS_READY_TIMEOUT_SECONDS = 45
WORKSPACE_BACKEND_CONNECTION_DRAIN_TIMEOUT_SECONDS = 2
DEFAULT_SSH_TUNNEL_PORT_MIN = 41000
DEFAULT_SSH_TUNNEL_PORT_MAX = 41999

logger = logging.getLogger(__name__)


class ManagedProcessHandle(Protocol):
    def request_terminate(self) -> None: ...

    def close(self, *, timeout_seconds: float = 8) -> None: ...

    def detach(self) -> None: ...


@dataclass(slots=True)
class ManagedProcess:
    process: subprocess.Popen[str]
    log_file: object

    def request_terminate(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self._terminate_group()
        except ProcessLookupError:
            return

    def close(self, *, timeout_seconds: float = 8) -> None:
        try:
            if self.process.poll() is None:
                self.request_terminate()
                try:
                    self.process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._kill_group()
                    self.process.wait(timeout=timeout_seconds)
        finally:
            close = getattr(self.log_file, "close", None)
            if callable(close):
                close()

    def detach(self) -> None:
        """Gateway 重启时放弃进程所有权，不向独立进程组发送信号。"""
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


@dataclass(frozen=True, slots=True)
class SshLocalForwardSpec:
    local_port: int
    remote_host: str
    remote_port: int


@dataclass(slots=True)
class AdoptedManagedProcess:
    """新 Gateway 通过已验证的健康接口重新接管存活的辅助服务。"""

    pid: int

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise ValueError(f"接管进程 PID 必须为正整数: {self.pid}")

    def request_terminate(self) -> None:
        if not self._is_alive():
            return
        try:
            if os.name == "posix":
                os.killpg(self.pid, signal.SIGTERM)
            else:
                os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def close(self, *, timeout_seconds: float = 8) -> None:
        self.request_terminate()
        deadline = time.monotonic() + timeout_seconds
        while self._is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self._is_alive():
            return
        try:
            if os.name == "posix":
                os.killpg(self.pid, signal.SIGKILL)
            else:
                os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def detach(self) -> None:
        return

    def _is_alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError as error:
            raise PermissionError(f"无权检查已接管辅助服务 PID: {self.pid}") from error
        return True


def _spawn_logged_process(
    command: list[str],
    *,
    log_file: TextIO,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    try:
        return subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=log_file,
            stderr=log_file,
            text=True,
            start_new_session=os.name == "posix",
        )
    except Exception:
        log_file.close()
        raise


def allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def is_local_port_available(port: int) -> bool:
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
        if is_local_port_available(port):
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


async def wait_for_http_ok(
    url: str,
    process: subprocess.Popen[str] | None = None,
) -> None:
    deadline = asyncio.get_running_loop().time() + GATEWAY_PROCESS_READY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=2) as client:
        while asyncio.get_running_loop().time() < deadline:
            if process is not None and process.poll() is not None:
                raise RuntimeError(
                    "进程提前退出: "
                    f"pid={process.pid}, returncode={process.returncode}, url={url}"
                )
            try:
                response = await client.get(
                    url,
                    headers={"X-Local-Token": "local-dev-token"},
                )
                if response.status_code == 200:
                    return
                last_error = RuntimeError(
                    f"健康检查返回 {response.status_code}: {response.text[:300]}"
                )
            except (httpx.HTTPError, RuntimeError) as error:
                last_error = error
            await asyncio.sleep(0.5)

    detail = f"，最后错误: {last_error}" if last_error else ""
    raise TimeoutError(
        f"目标服务在 {GATEWAY_PROCESS_READY_TIMEOUT_SECONDS} 秒内未就绪: "
        f"{url}{detail}"
    )


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
            "--timeout-graceful-shutdown",
            str(WORKSPACE_BACKEND_CONNECTION_DRAIN_TIMEOUT_SECONDS),
        ]
    )
    log_store = ProcessLogStore(log_dir)
    log_path = log_store.path_for(f"local-backend-{port}.log")
    log_file = log_store.open(log_path.name)
    process = _spawn_logged_process(
        command,
        log_file=log_file,
        cwd=project_root,
        env=env,
    )
    logger.info(
        "启动工作区后端: pid=%s port=%s workspace=%s log=%s",
        process.pid,
        port,
        workspace_root,
        log_path,
    )
    return ManagedProcess(process=process, log_file=log_file)


def start_local_node_service_process(
    *,
    project_root: Path,
    workspace_root: Path,
    workspace_id: str,
    service: str,
    port: int,
    log_dir: Path,
) -> ManagedProcess:
    if service not in {"terminal", "browser"}:
        raise ValueError(f"不支持的本地辅助服务: {service}")
    backend_path = project_root / "src" / service / "server" / "backend.js"
    if not backend_path.is_file():
        raise FileNotFoundError(f"辅助服务入口不存在: {backend_path}")
    node_executable = os.environ.get("BOXTEAM_NODE_BIN")
    if not node_executable:
        raise RuntimeError("启动本地辅助服务必须通过 BOXTEAM_NODE_BIN 显式提供 Node")
    log_store = ProcessLogStore(log_dir)
    log_path = log_store.path_for(f"local-{service}-{port}.log")
    log_file = log_store.open(log_path.name)
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    process = _spawn_logged_process(
        [
            node_executable,
            str(backend_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workspace-root",
            str(workspace_root),
            "--workspace-id",
            workspace_id,
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
        log_file=log_file,
        cwd=project_root,
        env=env,
    )
    logger.info(
        "启动工作区辅助服务: service=%s pid=%s port=%s workspace=%s log=%s",
        service,
        process.pid,
        port,
        workspace_root,
        log_path,
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
    command = build_ssh_command(
        host=host,
        port=port,
        username=username,
        private_key_path=(
            str(private_key_path) if private_key_path is not None else None
        ),
        ssh_config_host=ssh_config_host,
        extra_arguments=forward_arguments,
    )
    log_store = ProcessLogStore(log_dir)
    log_path = log_store.path_for(
        f"ssh-tunnel-{forwards[0].local_port}.log"
    )
    log_file = log_store.open(log_path.name)
    process = _spawn_logged_process(
        command,
        log_file=log_file,
    )
    logger.info(
        "启动 SSH 隧道: pid=%s host=%s port=%s forwards=%s log=%s",
        process.pid,
        host,
        port,
        len(forwards),
        log_path,
    )
    return ManagedProcess(process=process, log_file=log_file)


def _assert_ssh_config_has_no_forwards(
    *,
    host: str,
    port: int,
    username: str,
    private_key_path: Path | None,
    ssh_config_host: str | None,
) -> None:
    command = build_ssh_command(
        host=host,
        port=port,
        username=username,
        private_key_path=(
            str(private_key_path) if private_key_path is not None else None
        ),
        ssh_config_host=ssh_config_host,
        extra_arguments=["-G"],
    )
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "ssh -G 没有输出"
        raise RuntimeError(f"无法检查 SSH 有效配置: {detail}")
    forwarding_keys = {"localforward", "remoteforward", "dynamicforward"}
    configured = sorted(
        {
            line.split(maxsplit=1)[0].lower()
            for line in result.stdout.splitlines()
            if line.strip() and line.split(maxsplit=1)[0].lower() in forwarding_keys
        }
    )
    if configured:
        destination = ssh_config_host or f"{username}@{host}:{port}"
        raise ValueError(
            "SSH 有效配置包含转发指令，无法为每条工作区转发启动独立进程: "
            f"destination={destination}, options={','.join(configured)}。"
            "请创建一个不含 LocalForward/RemoteForward/DynamicForward 的专用 Host "
            "别名；ProxyJump、IdentityFile 等连接配置可以保留。"
        )


def start_workspace_ssh_port_forward_process(
    *,
    host: str,
    port: int,
    username: str,
    private_key_path: Path | None,
    ssh_config_host: str | None,
    forward: SshLocalForwardSpec,
    log_dir: Path,
    forward_id: str,
) -> tuple[ManagedProcess, Path]:
    _assert_ssh_config_has_no_forwards(
        host=host,
        port=port,
        username=username,
        private_key_path=private_key_path,
        ssh_config_host=ssh_config_host,
    )
    forward_arguments = [
        "-N",
        "-T",
        "-L",
        (
            f"127.0.0.1:{forward.local_port}:"
            f"{forward.remote_host}:{forward.remote_port}"
        ),
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ]
    command = build_ssh_command(
        host=host,
        port=port,
        username=username,
        private_key_path=(
            str(private_key_path) if private_key_path is not None else None
        ),
        ssh_config_host=ssh_config_host,
        extra_arguments=forward_arguments,
    )
    log_store = ProcessLogStore(log_dir)
    log_path = log_store.path_for(f"ssh-port-forward-{forward_id}.log")
    log_file = log_store.open(log_path.name)
    process = _spawn_logged_process(command, log_file=log_file)
    logger.info(
        "启动工作区 SSH 端口转发: pid=%s destination=%s local_port=%s "
        "remote_port=%s log=%s",
        process.pid,
        ssh_config_host or host,
        forward.local_port,
        forward.remote_port,
        log_path,
    )
    return ManagedProcess(process=process, log_file=log_file), log_path
