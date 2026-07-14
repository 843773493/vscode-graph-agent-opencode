from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from tests.e2e.processes import (
    kill_process_on_port,
    terminate_process,
    wait_for_http_ok,
)


@dataclass(frozen=True, slots=True)
class BrowserManagerProcesses:
    backend_process: subprocess.Popen[str]
    frontend_process: subprocess.Popen[str]
    backend_port: int
    frontend_port: int


def start_browser_manager_processes(
    *,
    workspace_root: Path,
    backend_port: int,
    frontend_port: int,
    backend_host: str,
) -> BrowserManagerProcesses:
    kill_process_on_port(backend_port)
    kill_process_on_port(frontend_port)
    project_root = Path.cwd().resolve()
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动浏览器管理器 e2e 进程")

    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    env["BOXTEAM_BROWSER_WORKSPACE_ROOT"] = str(workspace_root)

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
        cwd=project_root / "src" / "browser" / "server",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
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
        cwd=project_root / "src" / "browser" / "client",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    handle = BrowserManagerProcesses(
        backend_process=backend_process,
        frontend_process=frontend_process,
        backend_port=backend_port,
        frontend_port=frontend_port,
    )
    try:
        wait_for_http_ok(f"http://127.0.0.1:{backend_port}/health", backend_process)
        wait_for_http_ok(f"http://127.0.0.1:{frontend_port}/health", frontend_process)
    except Exception:
        close_browser_manager_processes(handle)
        raise
    return handle


def close_browser_manager_processes(handle: BrowserManagerProcesses) -> None:
    terminate_process(handle.frontend_process)
    terminate_process(handle.backend_process)
    kill_process_on_port(handle.frontend_port)
    kill_process_on_port(handle.backend_port)


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
