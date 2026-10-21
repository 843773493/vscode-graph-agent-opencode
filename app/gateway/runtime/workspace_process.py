from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from uuid import UUID

import httpx

from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.gateway.runtime.process_lifecycle import ManagedProcess, _spawn_logged_process
from app.gateway.runtime.process_logs import ProcessLogStore

GATEWAY_PROCESS_READY_TIMEOUT_SECONDS = 120
WORKSPACE_BACKEND_CONNECTION_DRAIN_TIMEOUT_SECONDS = 2
_GATEWAY_STARTUP_ENVIRONMENT_KEYS = (
    "BOXTEAM_CONFIG_CANDIDATE_REF",
    "BOXTEAM_CONFIG_GENERATION",
    "BOXTEAM_CONFIG_FENCING_TOKEN",
)

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


def resolve_python_executable() -> Path:
    configured = os.environ.get("BOXTEAM_PYTHON_BIN")
    if configured:
        executable = Path(configured).expanduser()
        is_absolute = (
            PureWindowsPath(executable).is_absolute()
            if os.name == "nt"
            else executable.is_absolute()
        )
        if not is_absolute:
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
            str(resolve_python_executable()),
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
    python_executable = resolve_python_executable()
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
