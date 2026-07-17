from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tests.e2e.processes import (
    kill_process_on_port,
    terminate_process,
    wait_for_http_ok,
)


@dataclass(frozen=True, slots=True)
class TerminalFrontendProcess:
    process: subprocess.Popen[str]
    port: int


def start_terminal_frontend_process(
    *,
    workspace_root: Path,
    frontend_port: int,
) -> TerminalFrontendProcess:
    kill_process_on_port(frontend_port)
    project_root = Path.cwd().resolve()
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动终端 attach 前端")
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = str(workspace_root)
    process = subprocess.Popen(
        [
            node_bin,
            "server.js",
            "--host",
            "127.0.0.1",
            "--port",
            str(frontend_port),
            "--backend-url",
            "http://127.0.0.1:8012",
            "--workspace-root",
            str(workspace_root),
            "--asset-root",
            str(project_root),
        ],
        cwd=project_root / "src" / "terminal" / "client",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    handle = TerminalFrontendProcess(process=process, port=frontend_port)
    try:
        wait_for_http_ok(f"http://127.0.0.1:{frontend_port}/health", process)
    except Exception:
        close_terminal_frontend_process(handle)
        raise
    return handle


def close_terminal_frontend_process(handle: TerminalFrontendProcess) -> None:
    terminate_process(handle.process)
    kill_process_on_port(handle.port)
