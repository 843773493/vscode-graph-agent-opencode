"""Terminal attach 前端的测试进程装具。"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.process_output import spawn_logged_process
from tests.support.processes import (
    ensure_local_port_available,
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
    artifacts_dir: Path,
    frontend_port: int,
) -> TerminalFrontendProcess:
    ensure_local_port_available(frontend_port, service_name="终端 attach 前端")
    project_root = Path.cwd().resolve()
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动终端 attach 前端")
    workspace_id = load_or_create_workspace_id(workspace_root)
    env = os.environ.copy()
    env["BOXTEAM_WORKSPACE_ID"] = workspace_id
    env["WORKSPACE_ROOT"] = str(workspace_root)
    process, logs = spawn_logged_process(
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
        cwd=project_root / "src" / "workspace-services" / "terminal" / "client",
        env=env,
        artifacts_dir=artifacts_dir,
        log_name="terminal-frontend",
    )
    handle = TerminalFrontendProcess(process=process, port=frontend_port)
    try:
        wait_for_http_ok(f"http://127.0.0.1:{frontend_port}/health", process)
    except BaseException as startup_error:
        for path in logs:
            startup_error.add_note(f"Terminal 测试进程日志：{path}")
        try:
            close_terminal_frontend_process(handle)
        except BaseException as cleanup_error:  # noqa: BLE001 - 启动和清理错误一并保留
            raise BaseExceptionGroup(
                "终端 attach 前端就绪检查失败且进程清理失败",
                [startup_error, cleanup_error],
            ) from None
        raise
    return handle


def close_terminal_frontend_process(handle: TerminalFrontendProcess) -> None:
    terminate_process(handle.process)
