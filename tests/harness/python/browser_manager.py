from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from app.core.workspace_identity import load_or_create_workspace_id
from tests.support.processes import (
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
    backend_port: int,
    frontend_port: int,
    backend_host: str,
) -> BrowserManagerProcesses:
    ensure_local_port_available(backend_port, service_name="浏览器管理器后端")
    ensure_local_port_available(frontend_port, service_name="浏览器管理器前端")
    project_root = Path.cwd().resolve()
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动浏览器管理器 e2e 进程")

    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_BROWSER_WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_WORKSPACE_ID"] = load_or_create_workspace_id(workspace_root)

    backend_process = subprocess.Popen(
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        frontend_process = subprocess.Popen(
            [
                node_bin,
                "server.js",
                "--host",
                "127.0.0.1",
                "--port",
                str(frontend_port),
                "--backend-url",
                f"http://127.0.0.1:{backend_port}",
                "--workspace-root",
                str(workspace_root),
                "--asset-root",
                str(project_root),
            ],
            cwd=project_root / "src" / "workspace-services" / "browser" / "client",
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except BaseException as startup_error:
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
        wait_for_http_ok(f"http://127.0.0.1:{backend_port}/health", backend_process)
        wait_for_http_ok(f"http://127.0.0.1:{frontend_port}/health", frontend_process)
    except BaseException as startup_error:
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
    frontend_port: int,
) -> BrowserFrontendProcess:
    ensure_local_port_available(frontend_port, service_name="浏览器管理器前端")
    project_root = Path.cwd().resolve()
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动浏览器 attach 前端")
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_WORKSPACE_ID"] = load_or_create_workspace_id(workspace_root)
    process = subprocess.Popen(
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    handle = BrowserFrontendProcess(process=process, port=frontend_port)
    try:
        wait_for_http_ok(f"http://127.0.0.1:{frontend_port}/health", process)
    except BaseException as startup_error:
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
