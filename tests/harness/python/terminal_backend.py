"""Terminal 后端的测试进程启动装具。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.process_output import spawn_logged_process
from tests.support.processes.workspace import (
    ensure_local_port_available,
    terminate_process,
    wait_for_http_ok,
)


def start_terminal_backend(
    *,
    backend_port: int,
    workspace_root: str,
    artifacts_dir: Path,
) -> subprocess.Popen[str]:
    ensure_local_port_available(backend_port, service_name="终端管理器后端")
    project_root = Path.cwd().resolve()
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动终端 e2e 进程")

    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = workspace_root
    env["BOXTEAM_TERMINAL_WORKSPACE_ROOT"] = workspace_root
    workspace_id = load_or_create_workspace_id(Path(workspace_root))
    process, logs = spawn_logged_process(
        [
            node_bin,
            "backend.js",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
            "--workspace-root",
            workspace_root,
            "--workspace-id",
            workspace_id,
        ],
        cwd=project_root / "src" / "workspace-services" / "terminal" / "server",
        env=env,
        artifacts_dir=artifacts_dir,
        log_name="terminal-backend",
    )
    try:
        wait_for_http_ok(
            f"http://127.0.0.1:{backend_port}/health", process, timeout_seconds=30
        )
    except BaseException as startup_error:
        for path in logs:
            startup_error.add_note(f"Terminal 测试进程日志：{path}")
        try:
            terminate_process(process)
        except BaseException as cleanup_error:  # noqa: BLE001 - 启动和清理错误一并保留
            raise BaseExceptionGroup(
                "终端管理器后端就绪检查失败且进程清理失败",
                [startup_error, cleanup_error],
            ) from None
        raise
    return process
