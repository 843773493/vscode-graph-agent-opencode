from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO
from uuid import UUID

import httpx

from app.gateway.runtime.process_groups import (
    _capture_posix_group_starttime,
    _posix_group_is_alive,
    _wait_until_closed,
)
from app.gateway.runtime.process_logs import ProcessLogStore
from app.gateway.service_types import LocalForwardSpec
from app.gateway.ssh_command import build_ssh_command

# 工作区后端需要完成 Python 冷启动导入和 lifespan 初始化；本地与 Windows
# 统一保留足够的就绪窗口，避免 Gateway 在后端即将就绪时提前清理进程组。
GATEWAY_PROCESS_READY_TIMEOUT_SECONDS = 120
WORKSPACE_BACKEND_CONNECTION_DRAIN_TIMEOUT_SECONDS = 2
DEFAULT_SSH_TUNNEL_PORT_MIN = 41000
DEFAULT_SSH_TUNNEL_PORT_MAX = 41999
_GATEWAY_STARTUP_ENVIRONMENT_KEYS = (
    "BOXTEAM_CONFIG_CANDIDATE_REF",
    "BOXTEAM_CONFIG_GENERATION",
    "BOXTEAM_CONFIG_FENCING_TOKEN",
)

from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry

logger = logging.getLogger(__name__)


def _local_child_environment() -> dict[str, str]:
    """构造本地子进程环境，隔离 Gateway 自身的 pending 启动契约。"""
    environment = os.environ.copy()
    for key in _GATEWAY_STARTUP_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    return environment


# TODO: Uvicorn 的命令行参数只接受整数秒；配置模型允许小数时向上取整，避免 Windows 启动时被 CLI 拒绝。
def _uvicorn_graceful_shutdown_timeout(seconds: float) -> str:
    return str(max(1, math.ceil(seconds)))


# TODO: Windows 的 taskkill 优雅模式对子进程返回非零是正常的，必须交给 close() 的 /F 阶段收尾。
def _taskkill_process_tree(pid: int, *, force: bool) -> None:
    arguments = ["taskkill", "/T"]
    if force:
        arguments.append("/F")
    arguments.extend(["/PID", str(pid)])
    result = subprocess.run(
        arguments,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0 and force:
        raise RuntimeError(
            "Windows 进程树清理失败: "
            f"pid={pid}, force={force}, stderr={result.stderr.strip()}"
        )


def _windows_process_is_alive(pid: int) -> bool:
    """用系统进程 API 查询 Windows PID，避免周期性启动 tasklist 子进程。"""
    import psutil

    try:
        return psutil.pid_exists(pid)
    except psutil.Error as error:
        raise RuntimeError(f"Windows 进程状态查询失败: pid={pid}: {error}") from error


class ManagedProcessHandle(Protocol):
    def is_alive(self) -> bool: ...

    def request_terminate(self) -> None: ...

    def close(self, *, timeout_seconds: float = 8) -> None: ...

    def detach(self) -> None: ...


@dataclass(slots=True)
class ManagedProcess:
    process: subprocess.Popen[str]
    log_file: object
    close_failure: TimeoutError | None = field(default=None, init=False, repr=False)
    _leader_starttime: int | None = field(default=None, init=False, repr=False)
    _group_closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if os.name == "posix":
            self._leader_starttime = _capture_posix_group_starttime(self.process.pid)

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def request_terminate(self) -> None:
        if not self._close_target_is_alive():
            return
        try:
            self._terminate_group()
        except ProcessLookupError:
            return

    def close(self, *, timeout_seconds: float = 8) -> None:
        if self.close_failure is not None:
            if self._close_target_is_alive():
                self._kill_group()
                self._wait_closed(timeout_seconds)
            raise self.close_failure
        if self._close_target_is_alive():
            self.request_terminate()
            try:
                self._wait_closed(timeout_seconds)
            except subprocess.TimeoutExpired as error:
                self.close_failure = TimeoutError(
                    "受管进程组未完成优雅关闭，已进入强制回收: "
                    f"pid={self.process.pid}, timeout_seconds={timeout_seconds}"
                )
                self.close_failure.__cause__ = error
                self._kill_group()
                self._wait_closed(timeout_seconds)
                raise self.close_failure from error
        # 关闭失败仍由原 owner 持有进程与日志句柄，避免重试丢失诊断输出。
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
            _taskkill_process_tree(self.process.pid, force=False)

    def _kill_group(self) -> None:
        if not self._close_target_is_alive():
            return
        if os.name == "posix":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        else:
            _taskkill_process_tree(self.process.pid, force=True)

    def _close_target_is_alive(self) -> bool:
        root_alive = self.process.poll() is None
        if os.name != "posix":
            return root_alive
        if not self._group_closed:
            self._group_closed = not _posix_group_is_alive(self.process.pid, self._leader_starttime)
        return not self._group_closed

    def _wait_closed(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        self.process.wait(timeout=timeout_seconds)
        # 根 wait 与剩余组成员共用原预算；根退出不能代表同组子进程已退出。
        if os.name == "posix" and not _wait_until_closed(self._close_target_is_alive, deadline=deadline):
            raise subprocess.TimeoutExpired(f"owned process group pid={self.process.pid}", timeout_seconds)


@dataclass(frozen=True, slots=True)
class SshLocalForwardSpec:
    local_port: int
    remote_host: str
    remote_port: int


@dataclass(slots=True)
class AdoptedManagedProcess:
    """新 Gateway 通过已验证的健康接口重新接管存活的辅助服务。"""

    pid: int
    close_failure: TimeoutError | None = field(default=None, init=False, repr=False)
    _leader_starttime: int | None = field(default=None, init=False, repr=False)
    _group_closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise ValueError(f"接管进程 PID 必须为正整数: {self.pid}")
        if os.name == "posix":
            self._leader_starttime = _capture_posix_group_starttime(self.pid)

    def is_alive(self) -> bool:
        return self._is_alive()

    def request_terminate(self) -> None:
        if not self._close_target_is_alive():
            return
        try:
            if os.name == "posix":
                os.killpg(self.pid, signal.SIGTERM)
            else:
                _taskkill_process_tree(self.pid, force=False)
        except ProcessLookupError:
            return

    def close(self, *, timeout_seconds: float = 8) -> None:
        if self.close_failure is None:
            self.request_terminate()
            deadline = time.monotonic() + timeout_seconds
            if _wait_until_closed(self._close_target_is_alive, deadline=deadline):
                return
            self.close_failure = TimeoutError(
                "已接管进程组未完成优雅关闭，已进入强制回收: "
                f"pid={self.pid}, timeout_seconds={timeout_seconds}"
            )
        if self._close_target_is_alive():
            try:
                if os.name == "posix":
                    os.killpg(self.pid, signal.SIGKILL)
                else:
                    _taskkill_process_tree(self.pid, force=True)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + timeout_seconds
            if not _wait_until_closed(self._close_target_is_alive, deadline=deadline):
                raise RuntimeError(f"已接管进程组强制终止后仍未退出: pid={self.pid}") from self.close_failure
        # 强制回收不能证明 unload 已完成，后续关闭也不得洗掉这一失败事实。
        raise self.close_failure

    def detach(self) -> None:
        return

    def _is_alive(self) -> bool:
        if os.name == "nt":
            return _windows_process_is_alive(self.pid)
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError as error:
            raise PermissionError(f"无权检查已接管辅助服务 PID: {self.pid}") from error
        return True

    def _close_target_is_alive(self) -> bool:
        if os.name != "posix":
            return self._is_alive()
        if not self._group_closed:
            self._group_closed = not _posix_group_is_alive(self.pid, self._leader_starttime)
        return not self._group_closed


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
        # 发布版默认 Workspace 固定端口在重启后可能短暂处于 TIME_WAIT；POSIX
        # 服务端同样允许复用该地址，探测必须与实际监听语义保持一致。
        if os.name == "posix":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
        # TODO: Windows 将无盘符根路径（如 /workspace）表示为 rooted path，不能只依赖 pathlib.is_absolute。
        if not os.path.isabs(str(executable)):
            raise ValueError(f"BOXTEAM_PYTHON_BIN 必须是绝对路径: {configured}")
        # 不解引用 venv 的 python 符号链接；真实路径会丢失 pyvenv.cfg 上下文。
        return executable
    return Path(sys.executable).absolute()


def resolve_backend_workspace_id(project_root: Path, workspace_root: Path, *, initialize: bool = True) -> str:
    """通过 Workspace CLI 取得 UUID；恢复必须只读，不得重建缺失身份。"""

    resolved_project_root = project_root.expanduser().resolve(strict=True)
    resolved_workspace_root = workspace_root.expanduser().resolve(strict=True)
    if not resolved_workspace_root.is_dir():
        raise NotADirectoryError(f"工作区根不是目录: {resolved_workspace_root}")
    result = subprocess.run(
        [
            str(resolve_python_executable(resolved_project_root)),
            "-m",
            "app.core.workspace_identity",
            "--workspace-root",
            str(resolved_workspace_root),
            *([] if initialize else ["--read-only"]),
        ],
        cwd=resolved_project_root,
        env=_local_child_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Workspace backend UUID CLI 执行失败: "
            f"workspace_root={resolved_workspace_root}, exit={result.returncode}, "
            f"stderr={result.stderr!r}, stdout={result.stdout!r}"
        )
    if result.stderr:
        raise RuntimeError(
            "Workspace backend UUID CLI 成功时 stderr 必须为空: "
            f"workspace_root={resolved_workspace_root}, stderr={result.stderr!r}"
        )
    if not result.stdout.endswith("\n") or result.stdout.count("\n") != 1:
        raise TypeError(
            "Workspace backend UUID CLI 必须只输出一行 UUID: "
            f"workspace_root={resolved_workspace_root}, stdout={result.stdout!r}"
        )
    workspace_id = result.stdout[:-1]
    try:
        parsed_workspace_id = UUID(workspace_id)
    except ValueError as error:
        raise ValueError(
            "Workspace backend UUID CLI 返回非 UUID: "
            f"workspace_root={resolved_workspace_root}, workspace_id={workspace_id!r}"
        ) from error
    if str(parsed_workspace_id) != workspace_id:
        raise ValueError(
            "Workspace backend UUID CLI 返回非标准 UUID 文本: "
            f"workspace_root={resolved_workspace_root}, workspace_id={workspace_id!r}"
        )
    return workspace_id


async def wait_for_http_ok(
    url: str,
    process: subprocess.Popen[str] | None = None,
    *,
    request_timeout_seconds: float = 2,
    poll_interval_seconds: float = 0.5,
) -> None:
    deadline = asyncio.get_running_loop().time() + GATEWAY_PROCESS_READY_TIMEOUT_SECONDS
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=request_timeout_seconds) as client:
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
            await asyncio.sleep(poll_interval_seconds)

    detail = f"，最后错误: {last_error}" if last_error else ""
    raise TimeoutError(
        f"目标服务在 {GATEWAY_PROCESS_READY_TIMEOUT_SECONDS} 秒内未就绪: {url}{detail}"
    )


def start_local_backend_process(
    *,
    project_root: Path,
    mounts: tuple[MountedWorkspace, ...],
    backend_generation: str,
    port: int,
    log_dir: Path,
    extra_env: dict[str, str] | None = None,
    debug_port: int | None = None,
    asgi_app: str = "app.main:app",
    connection_drain_timeout_seconds: float = (
        WORKSPACE_BACKEND_CONNECTION_DRAIN_TIMEOUT_SECONDS
    ),
) -> ManagedProcess:
    mounted = MountedWorkspaceRegistry(list(mounts))
    if not backend_generation.strip():
        raise ValueError("启动 Backend 必须提供 generation")
    python_executable = resolve_python_executable(project_root)
    env = _local_child_environment()
    env["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    env["BOXTEAM_BACKEND_GENERATION"] = backend_generation
    env["BOXTEAM_MOUNTED_WORKSPACES"] = json.dumps(
        [mount.to_payload() for mount in mounted.mounts], separators=(",", ":"),
    )
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
            asgi_app,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--timeout-graceful-shutdown",
            _uvicorn_graceful_shutdown_timeout(connection_drain_timeout_seconds),
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
        ",".join(mounted.workspace_ids),
        log_path,
    )
    return ManagedProcess(process=process, log_file=log_file)


def start_local_node_service_process(
    *,
    project_root: Path,
    workspace_root: Path,
    service: str,
    port: int,
    log_dir: Path,
    workspace_id: str,
) -> ManagedProcess:
    if service not in {"terminal", "browser"}:
        raise ValueError(f"不支持的本地辅助服务: {service}")
    backend_path = (
        project_root / "src" / "workspace-services" / service / "server" / "backend.js"
    )
    if not backend_path.is_file():
        raise FileNotFoundError(f"辅助服务入口不存在: {backend_path}")
    node_executable = os.environ.get("BOXTEAM_NODE_BIN")
    if not node_executable:
        raise RuntimeError("启动本地辅助服务必须通过 BOXTEAM_NODE_BIN 显式提供 Node")
    log_store = ProcessLogStore(log_dir)
    log_path = log_store.path_for(f"local-{service}-{port}.log")
    log_file = log_store.open(log_path.name)
    env = _local_child_environment()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    command = [
        node_executable,
        str(backend_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workspace-root",
        str(workspace_root),
    ]
    parsed_workspace_id = UUID(workspace_id)
    if str(parsed_workspace_id) != workspace_id:
        raise ValueError("辅助服务必须提供标准 backend UUID 文本")
    command.extend(["--workspace-id", workspace_id])
    if service == "browser":
        command.extend(
            ["--frontend-url", os.environ.get("BOXTEAM_BROWSER_FRONTEND_URL", "http://127.0.0.1")]
        )
    process = _spawn_logged_process(
        command,
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
    log_path = log_store.path_for(f"ssh-tunnel-{forwards[0].local_port}.log")
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
        (f"127.0.0.1:{forward.local_port}:{forward.remote_host}:{forward.remote_port}"),
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
