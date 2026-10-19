from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from urllib.request import Request, urlopen

import commentjson
import httpx

from configs.installer import install_user_configuration
from configs.runtime import load_validated_config
from tests.support.process_scope import register_test_process, unregister_test_process
from tests.support.processes import (
    ensure_local_port_available,
    resolve_workspace_python_executable,
    terminate_process,
)

LOCAL_TOKEN_HEADERS = {"X-Local-Token": "local-dev-token"}
READY_TIMEOUT_SECONDS = 60


def _test_boxteam_home(workspace_root: Path) -> Path:
    return workspace_root.resolve().parent / "boxteam-home"


def reset_gateway_persistent_state(*, workspace_root: Path) -> None:
    """删除隔离工作区旁的 BOXTEAM_HOME，清空 Gateway 跨运行持久状态。

    工作区注册表（gateway.sqlite）、导航树、会话目录缓存和生成器定义都持久化在
    ``<boxteam-home>/state/gateway``，与工作区一样跨测试运行存活。工作区后端端口
    由测试文件在 ``tests/`` 下的排序位置决定，测试文件集合变化会让同一工作区路径
    产生不同的 workspace_id 注册项；上一次运行遗留的注册项被本次 Gateway 加载后，
    导航节点集合与跨工作区目录聚合结果会多算一份。重建工作区前必须清掉它。

    这里整体删除 BOXTEAM_HOME 而不是只删 ``state/gateway``：``config/gateway.jsonc``
    同样跨运行留存，测试会把它改成非默认值（poll_interval_seconds、远程 Gateway
    声明等），只清 state 会让这些残留继续被下一次运行加载。``auth/`` 属附带删除，
    integration 用例不配置 ChatGPT OAuth provider，凭据目录按需重新迁移。
    """

    boxteam_home = _test_boxteam_home(workspace_root)
    if boxteam_home.exists():
        shutil.rmtree(boxteam_home)


def _install_current_user_configuration(
    *,
    config_root: Path,
    project_root: Path,
    force: bool,
) -> None:
    """安装当前用户配置；上次运行遗留的旧键被新 schema 拒绝时强制刷新。

    测试内手动编辑配置并重启 Gateway 的用例依赖配置跨重启保留，
    因此只有配置校验失败（schema 演进后的陈旧残留）才回退到内置配置。
    """

    install_user_configuration(
        config_root=config_root,
        profile="default",
        project_root=project_root,
        force=force,
    )
    if force:
        return
    for domain in ("gateway", "workspace"):
        config_path = config_root / f"{domain}.jsonc"
        schema_path = config_root / f"{domain}_schema.jsonc"
        try:
            load_validated_config(config_path=config_path, schema_path=schema_path)
        except (OSError, TypeError, ValueError):
            install_user_configuration(
                config_root=config_root,
                profile="default",
                project_root=project_root,
                force=True,
            )
            return


@dataclass(frozen=True, slots=True)
class GatewayProcess:
    process: subprocess.Popen[str]
    stdout_file: IO[str]
    stderr_file: IO[str]
    port: int


async def acquire_gateway_guest(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/gateway/users/guest",
        json={"tracking": {"test": True}},
    )
    if response.status_code != 200:
        raise RuntimeError(f"Gateway 测试游客登录失败: {response.text}")


def wait_for_gateway_ready(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    request = Request(
        f"http://127.0.0.1:{port}/api/gateway/health",
        headers=LOCAL_TOKEN_HEADERS,
    )
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Gateway 提前退出: pid={process.pid}, returncode={process.returncode}"
            )
        try:
            with urlopen(request, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(1)
    raise TimeoutError(f"Gateway 在 {READY_TIMEOUT_SECONDS} 秒内未就绪: {port}")


def start_gateway_process(
    *,
    workspace_root: Path,
    default_backend_url: str | None,
    port: int,
    ssh_tunnel_port_range: tuple[int, int] | None = None,
    extra_env: dict[str, str] | None = None,
    refresh_config: bool = False,
    boxteam_home: Path | None = None,
    artifacts_dir: Path | None = None,
    asgi_app: str = "app.gateway.main:app",
) -> GatewayProcess:
    ensure_local_port_available(port, service_name="Gateway")
    project_root = Path.cwd().resolve()
    python_executable = resolve_workspace_python_executable(project_root)
    boxteam_home = boxteam_home or _test_boxteam_home(workspace_root)
    _install_current_user_configuration(
        config_root=boxteam_home / "config",
        project_root=project_root,
        force=refresh_config,
    )
    log_dir = artifacts_dir or workspace_root / ".boxteam" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_file = open(  # noqa: SIM115
        log_dir / "gateway.stdout.log", "a", encoding="utf-8"
    )
    stderr_file = open(  # noqa: SIM115
        log_dir / "gateway.stderr.log", "a", encoding="utf-8"
    )
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_HOME"] = str(boxteam_home)
    # 测试 Gateway 的全局状态必须跟随隔离的 BOXTEAM_HOME，不能继承宿主环境的路径。
    env.pop("BOXTEAM_GATEWAY_ROOT", None)
    env["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    env["BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_GATEWAY_URL"] = f"http://127.0.0.1:{port}"
    env.pop("BOXTEAM_DEFAULT_BACKEND_URL", None)
    env["PYTHONUNBUFFERED"] = "1"
    env["BOXTEAM_PYTHON_BIN"] = str(python_executable)
    node_executable = shutil.which("node")
    if node_executable is None:
        raise RuntimeError("Gateway 测试需要 PATH 中存在 node")
    env["BOXTEAM_NODE_BIN"] = node_executable
    gateway_root = boxteam_home / "state" / "gateway"
    credential_path = gateway_root / "credentials" / "local-token"
    credential_path.parent.mkdir(parents=True, exist_ok=True)
    credential_path.write_text("local-dev-token\n", encoding="utf-8")
    credential_path.chmod(0o600)
    if ssh_tunnel_port_range is not None:
        env["BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MIN"] = str(ssh_tunnel_port_range[0])
        env["BOXTEAM_GATEWAY_SSH_TUNNEL_PORT_MAX"] = str(ssh_tunnel_port_range[1])
    if extra_env:
        env.update(extra_env)
    if default_backend_url is not None:
        if not default_backend_url.strip():
            raise ValueError("Gateway attached backend URL 不能为空")
        env["BOXTEAM_DEFAULT_BACKEND_URL"] = default_backend_url
    else:
        env.pop("BOXTEAM_DEFAULT_BACKEND_URL", None)
    process = subprocess.Popen(
        [
            str(python_executable),
            "-m",
            "uvicorn",
            asgi_app,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--timeout-graceful-shutdown",
            "2",
            "--log-level",
            "warning",
        ],
        cwd=project_root,
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
    )
    handle = GatewayProcess(
        process=process,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
        port=port,
    )
    try:
        wait_for_gateway_ready(port, process)
    except BaseException as startup_error:
        try:
            close_gateway_process(handle)
        except BaseException as cleanup_error:  # noqa: BLE001 - 启动和清理错误一并保留
            raise BaseExceptionGroup(
                "Gateway 测试进程启动失败且清理已启动进程失败",
                [startup_error, cleanup_error],
            ) from None
        raise
    register_test_process(
        handle,
        lambda: close_gateway_process(handle),
    )
    return handle


def close_gateway_process(handle: GatewayProcess) -> None:
    try:
        terminate_process(handle.process)
    finally:
        handle.stdout_file.close()
        handle.stderr_file.close()
    unregister_test_process(handle)


def workspace_id_from_response(response: httpx.Response) -> str:
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "root_path" not in data
    assert "root" not in data["git"]
    return str(data["workspace_id"])


def reset_gateway_user_configuration(*, workspace_root: Path) -> None:
    """重建测试 BOXTEAM_HOME 的用户配置，移除测试写入的远程 Gateway 声明。

    测试声明的远程 Gateway 会留在用户配置里跨运行生效；只有显式重建用户配置
    才能让它不再被后续 Gateway 启动重新加载。
    """

    install_user_configuration(
        config_root=_test_boxteam_home(workspace_root) / "config",
        profile="default",
        project_root=Path.cwd(),
        force=True,
    )


def write_gateway_remote_gateway_config(
    *,
    workspace_root: Path,
    ssh_port: int,
    username: str,
    remote_gateway_port: int,
    private_key_path: Path,
    remote_pair_command: str,
) -> None:
    config_root = _test_boxteam_home(workspace_root) / "config"
    install_user_configuration(
        config_root=config_root,
        profile="default",
        project_root=Path.cwd(),
    )
    config_path = config_root / "gateway.jsonc"
    if not config_path.is_file():
        raise FileNotFoundError(f"Gateway 测试工作区配置不存在: {config_path}")
    payload = commentjson.loads(config_path.read_text(encoding="utf-8"))
    payload["workspaces"] = [
        {
            "kind": "remote_gateway",
            "name": "remote docker gateway",
            "host": "127.0.0.1",
            "port": ssh_port,
            "username": username,
            "private_key_path": str(private_key_path.resolve()),
            "remote_pair_command": remote_pair_command,
            "remote_gateway_port": remote_gateway_port,
            "activate": False,
        }
    ]
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
