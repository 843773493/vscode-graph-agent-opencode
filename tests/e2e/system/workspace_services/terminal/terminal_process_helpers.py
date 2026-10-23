from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psutil

from app.core.identifier import create_uuid_hex
from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import TEST_FILE_PORT_BLOCK_SIZE
from tests.support.workspaces import prepare_test_workspace


def _terminal_port_pair(e2e_backend_port: int, offset: int) -> tuple[int, int]:
    if offset < 0 or offset + 1 >= TEST_FILE_PORT_BLOCK_SIZE:
        raise ValueError(
            f"终端 e2e 端口偏移超出当前测试文件端口块: offset={offset}, block_size={TEST_FILE_PORT_BLOCK_SIZE}"
        )
    return e2e_backend_port + offset, e2e_backend_port + offset + 1


def terminal_ports(e2e_backend_port: int) -> tuple[int, int]:
    return _terminal_port_pair(e2e_backend_port, 40)


def terminal_ports_with_offset(e2e_backend_port: int, offset: int) -> tuple[int, int]:
    return _terminal_port_pair(e2e_backend_port, offset)


def create_terminal_scenario_workspace(
    base_workspace_root: str,
    scenario_name: str,
) -> str:
    workspace_root = (
        Path(base_workspace_root).resolve()
        / f"terminal_lifecycle_{scenario_name}"
    )
    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True)
    return str(workspace_root)


def prepare_empty_terminal_e2e_workspace(test_file: Path) -> str:
    """只创建终端 E2E 需要的空工作区和日志目录。"""
    context = TestRunContext.from_test_file(test_file)
    context.artifacts_dir.mkdir(parents=True, exist_ok=True)
    workspace_root = prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=(
            context.project_root
            / "tests"
            / "fixtures"
            / "workspaces"
            / "default_test_workspace"
        ),
        template_items=(),
    )
    return str(workspace_root)


def create_long_running_terminal(
    *,
    backend_port: int,
    workspace_root: str,
    title: str,
) -> dict[str, object]:
    workspace_id = load_or_create_workspace_id(Path(workspace_root))
    payload = {
        "workspace_id": workspace_id,
        "terminal_id": f"term_{create_uuid_hex()}",
        "owner_revision": 1,
        "title": title,
        "cwd": str(Path(workspace_root).resolve()),
        "command": "/bin/sh",
        "args": ["-c", "trap '' TERM HUP INT; while true; do sleep 1; done"],
    }
    request = Request(
        f"http://127.0.0.1:{backend_port}/api/terminals",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-terminal"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))["data"]
    except HTTPError as error:
        with error:
            detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"创建终端失败: status={error.code}, body={detail}"
        ) from error


def get_terminal_snapshot(backend_port: int, terminal_id: str, workspace_id: str) -> dict[str, object]:
    request = Request(
        f"http://127.0.0.1:{backend_port}/api/terminals/{terminal_id}",
        headers={"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Resource-Owner": "workspace-terminal"},
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))["data"]


def wait_terminal_snapshot(
    backend_port: int,
    terminal_id: str,
    workspace_id: str,
    *,
    timeout_seconds: float = 5,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return get_terminal_snapshot(backend_port, terminal_id, workspace_id)
        except HTTPError as error:
            if error.code != 404:
                raise
            last_error = error
            time.sleep(0.1)
    raise TimeoutError(f"终端快照未恢复: terminal_id={terminal_id}") from last_error


def wait_terminal_process_metadata(backend_port: int, terminal_id: str, workspace_id: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = get_terminal_snapshot(backend_port, terminal_id, workspace_id)
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
    return psutil.pid_exists(pid)


def wait_process_gone(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not process_exists(pid):
            return
        time.sleep(0.1)
    raise AssertionError(f"进程仍然存在: pid={pid}")
