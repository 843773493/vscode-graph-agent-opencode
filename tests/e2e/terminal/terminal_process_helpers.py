from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from langchain_core.messages import HumanMessage, ToolMessage

from app.core.checkpoint_config import build_checkpoint_config
from app.core.checkpoint_saver import FileSystemCheckpointSaver
from tests.e2e.ports import E2E_PORT_BLOCK_SIZE
from tests.e2e.processes import kill_process_on_port, terminate_process


def _terminal_port_pair(e2e_backend_port: int, offset: int) -> tuple[int, int]:
    if offset < 0 or offset + 1 >= E2E_PORT_BLOCK_SIZE:
        raise ValueError(
            f"终端 e2e 端口偏移超出当前测试文件端口块: offset={offset}, block_size={E2E_PORT_BLOCK_SIZE}"
        )
    return e2e_backend_port + offset, e2e_backend_port + offset + 1


def terminal_ports(e2e_backend_port: int) -> tuple[int, int]:
    return _terminal_port_pair(e2e_backend_port, 40)


def terminal_ports_with_offset(e2e_backend_port: int, offset: int) -> tuple[int, int]:
    return _terminal_port_pair(e2e_backend_port, offset)


def seed_historical_terminal_checkpoint(
    *,
    workspace_root: str,
    session_id: str,
    terminal_id: str,
    frontend_port: int,
) -> None:
    saver = FileSystemCheckpointSaver(
        sessions_dir=Path(workspace_root) / ".boxteam" / "sessions"
    )
    terminal_payload = {
        "status": "background",
        "terminal_id": terminal_id,
        "session_id": session_id,
        "command": "while true; do echo HISTORICAL_TERMINAL; sleep 2; done",
        "attach_url": f"http://127.0.0.1:{frontend_port}/?terminalId={terminal_id}",
        "terminal": {
            "terminal_id": terminal_id,
            "session_id": session_id,
            "title": "historical terminal",
            "command": "/bin/bash",
            "args": ["-i"],
            "cwd": str(Path(workspace_root).resolve()),
            "cols": 100,
            "rows": 30,
            "status": "running",
            "created_at": "2026-07-05T16:40:37.385Z",
            "updated_at": "2026-07-05T16:40:47.427Z",
            "started_at": "2026-07-05T16:40:37.387Z",
            "ended_at": None,
            "last_command": "while true; do echo HISTORICAL_TERMINAL; sleep 2; done",
            "last_command_status": "running",
            "last_command_exit_code": None,
            "last_command_started_at": "2026-07-05T16:40:37.390Z",
            "last_command_completed_at": None,
            "attach_url": f"http://127.0.0.1:{frontend_port}/?terminalId={terminal_id}",
        },
    }
    messages_version = saver.get_next_version(None, None)
    checkpoint = {
        "id": str(uuid.uuid4()),
        "channel_values": {
            "messages": [
                HumanMessage(content="请启动一个历史终端"),
                ToolMessage(
                    content=(
                        "Tool call persistent_terminal with id call_cancelled "
                        "was cancelled - another message came in before it could be completed."
                    ),
                    tool_call_id="call_cancelled",
                    name="persistent_terminal",
                ),
                ToolMessage(
                    content=json.dumps(terminal_payload),
                    tool_call_id="call_historical_terminal",
                    name="persistent_terminal",
                ),
            ]
        },
        "channel_versions": {"messages": messages_version},
        "updated_channels": ["messages"],
    }
    saver.put(
        build_checkpoint_config(session_id),
        checkpoint,
        metadata={"source": "e2e_seed", "step": -1, "writes": {}},
        new_versions={"messages": messages_version},
    )


def wait_for_http_ok(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise RuntimeError(
                f"终端测试进程提前退出: {url}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise TimeoutError(f"终端测试进程未就绪: {url}")


def start_terminal_backend(
    *,
    backend_port: int,
    frontend_port: int,
    workspace_root: str,
) -> subprocess.Popen[str]:
    project_root = Path.cwd().resolve()
    node_bin = shutil.which("node")
    if node_bin is None:
        raise RuntimeError("未找到 node，无法启动终端 e2e 进程")

    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = workspace_root
    env["BOXTEAM_TERMINAL_WORKSPACE_ROOT"] = workspace_root
    process = subprocess.Popen(
        [
            node_bin,
            "backend.js",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
            "--frontend-url",
            f"http://127.0.0.1:{frontend_port}",
            "--workspace-root",
            workspace_root,
        ],
        cwd=project_root / "src" / "terminal" / "server",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wait_for_http_ok(f"http://127.0.0.1:{backend_port}/health", process)
    return process


def lifecycle_workspace(base_workspace_root: str, name: str) -> str:
    workspace_root = Path(base_workspace_root).resolve() / f"terminal_lifecycle_{name}"
    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True)
    return str(workspace_root)


def create_long_running_terminal(
    *,
    backend_port: int,
    workspace_root: str,
    session_id: str,
) -> dict[str, object]:
    payload = {
        "session_id": session_id,
        "title": "lifecycle cleanup test",
        "cwd": str(Path(workspace_root).resolve()),
        "command": "/bin/sh",
        "args": ["-c", "trap '' TERM HUP INT; while true; do sleep 1; done"],
    }
    request = Request(
        f"http://127.0.0.1:{backend_port}/api/terminals",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))["data"]


def get_terminal_snapshot(backend_port: int, terminal_id: str) -> dict[str, object]:
    with urlopen(f"http://127.0.0.1:{backend_port}/api/terminals/{terminal_id}", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))["data"]


def wait_terminal_snapshot(
    backend_port: int,
    terminal_id: str,
    *,
    timeout_seconds: float = 5,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return get_terminal_snapshot(backend_port, terminal_id)
        except HTTPError as error:
            if error.code != 404:
                raise
            last_error = error
            time.sleep(0.1)
    raise TimeoutError(f"终端快照未恢复: terminal_id={terminal_id}") from last_error


def wait_terminal_process_metadata(backend_port: int, terminal_id: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = get_terminal_snapshot(backend_port, terminal_id)
        if snapshot.get("process_session_id") and snapshot.get("process_start_time"):
            return
        time.sleep(0.1)
    raise TimeoutError(f"终端进程元数据未写入: {terminal_id}")


def terminal_state_record(workspace_root: str, terminal_id: str) -> dict[str, object]:
    state_file = Path(workspace_root) / ".boxteam" / "terminal-manager" / "terminals.json"
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    for terminal in raw["terminals"]:
        if terminal.get("terminal_id") == terminal_id:
            return terminal
    raise AssertionError(f"终端状态记录不存在: {terminal_id}")


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_process_gone(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not process_exists(pid):
            return
        time.sleep(0.1)
    raise AssertionError(f"进程仍然存在: pid={pid}")
