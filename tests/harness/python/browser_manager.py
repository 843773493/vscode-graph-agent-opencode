from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.process_output import spawn_logged_process
from tests.support.processes.workspace import (
    ensure_local_port_available,
    terminate_process,
    wait_for_http_ok,
)


@dataclass(frozen=True, slots=True)
class BrowserManagerProcesses:
    backend_process: subprocess.Popen[str]
    frontend_process: subprocess.Popen[str]
    backend_port: int
    frontend_port: int


@dataclass(frozen=True, slots=True)
class BrowserFrontendProcess:
    process: subprocess.Popen[str]
    port: int


def start_browser_manager_processes(
    *,
    workspace_root: Path,
    artifacts_dir: Path,
    backend_port: int,
    frontend_port: int,
    backend_host: str,
) -> BrowserManagerProcesses:
    ensure_local_port_available(backend_port, service_name="浏览器管理器后端", host=backend_host)
    ensure_local_port_available(frontend_port, service_name="浏览器管理器前端")
    # 通配地址用于监听，本机访问用同地址族回环；IPv6仅在URL authority中加方括号。
    backend_connect_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(backend_host, backend_host)
    backend_url_host = f"[{backend_connect_host}]" if ":" in backend_connect_host else backend_connect_host
    backend_url = f"http://{backend_url_host}:{backend_port}"
    project_root = Path.cwd().resolve()
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动浏览器管理器 e2e 进程")

    env = os.environ.copy()
    env["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_BROWSER_WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_WORKSPACE_ID"] = load_or_create_workspace_id(workspace_root)

    backend_process, backend_logs = spawn_logged_process(
        [
            node_bin,
            "backend.js",
            "--host",
            backend_host,
            "--port",
            str(backend_port),
            "--frontend-url",
            f"http://127.0.0.1:{frontend_port}",
            "--workspace-root",
            str(workspace_root),
        ],
        cwd=project_root / "src" / "workspace-services" / "browser" / "server",
        env=env,
        artifacts_dir=artifacts_dir,
        log_name="browser-backend",
    )
    try:
        frontend_process, frontend_logs = spawn_logged_process(
            [
                node_bin,
                "server.js",
                "--host",
                "127.0.0.1",
                "--port",
                str(frontend_port),
                "--backend-url",
                backend_url,
                "--workspace-root",
                str(workspace_root),
                "--asset-root",
                str(project_root),
            ],
            cwd=project_root / "src" / "workspace-services" / "browser" / "client",
            env=env,
            artifacts_dir=artifacts_dir,
            log_name="browser-frontend",
        )
    except BaseException as startup_error:
        for path in backend_logs:
            startup_error.add_note(f"Browser 测试进程日志：{path}")
        try:
            terminate_process(backend_process)
        except BaseException as cleanup_error:  # noqa: BLE001 - 两个错误都必须保留
            raise BaseExceptionGroup(
                "浏览器管理器前端启动失败且后端进程清理失败",
                [startup_error, cleanup_error],
            ) from None
        raise
    handle = BrowserManagerProcesses(
        backend_process=backend_process,
        frontend_process=frontend_process,
        backend_port=backend_port,
        frontend_port=frontend_port,
    )
    try:
        wait_for_http_ok(f"{backend_url}/health", backend_process)
        wait_for_http_ok(f"http://127.0.0.1:{frontend_port}/health", frontend_process)
    except BaseException as startup_error:
        for path in (*backend_logs, *frontend_logs):
            startup_error.add_note(f"Browser 测试进程日志：{path}")
        try:
            close_browser_manager_processes(handle)
        except BaseException as cleanup_error:  # noqa: BLE001 - 两个错误都必须保留
            raise BaseExceptionGroup(
                "浏览器管理器就绪检查失败且进程清理失败",
                [startup_error, cleanup_error],
            ) from None
        raise
    return handle


def close_browser_manager_processes(handle: BrowserManagerProcesses) -> None:
    errors: list[BaseException] = []
    for process in (handle.frontend_process, handle.backend_process):
        try:
            terminate_process(process)
        except BaseException as error:  # noqa: BLE001 - 后续 owned 进程仍须清理
            errors.append(error)
    if errors:
        raise BaseExceptionGroup("浏览器管理器进程清理失败", errors)


def start_browser_frontend_process(
    *,
    workspace_root: Path,
    artifacts_dir: Path,
    frontend_port: int,
) -> BrowserFrontendProcess:
    ensure_local_port_available(frontend_port, service_name="浏览器管理器前端")
    project_root = Path.cwd().resolve()
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动浏览器 attach 前端")
    env = os.environ.copy()
    env["BOXTEAM_PROJECT_ROOT"] = str(project_root)
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_WORKSPACE_ID"] = load_or_create_workspace_id(workspace_root)
    process, logs = spawn_logged_process(
        [
            node_bin,
            "server.js",
            "--host",
            "127.0.0.1",
            "--port",
            str(frontend_port),
            "--backend-url",
            "http://127.0.0.1:8015",
            "--workspace-root",
            str(workspace_root),
            "--asset-root",
            str(project_root),
        ],
        cwd=project_root / "src" / "workspace-services" / "browser" / "client",
        env=env,
        artifacts_dir=artifacts_dir,
        log_name="browser-frontend",
    )
    handle = BrowserFrontendProcess(process=process, port=frontend_port)
    try:
        wait_for_http_ok(f"http://127.0.0.1:{frontend_port}/health", process)
    except BaseException as startup_error:
        for path in logs:
            startup_error.add_note(f"Browser 测试进程日志：{path}")
        try:
            close_browser_frontend_process(handle)
        except BaseException as cleanup_error:  # noqa: BLE001 - 两个错误都必须保留
            raise BaseExceptionGroup(
                "浏览器 attach 前端就绪检查失败且进程清理失败",
                [startup_error, cleanup_error],
            ) from None
        raise
    return handle


def close_browser_frontend_process(handle: BrowserFrontendProcess) -> None:
    terminate_process(handle.process)


def browser_test_data_url() -> str:
    html = """
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>Gateway Docker Browser</title>
  </head>
  <body>
    <h1>Gateway Docker Browser Resource</h1>
    <button id="ok">OK</button>
  </body>
</html>
"""
    return "data:text/html;charset=utf-8," + quote(html)
