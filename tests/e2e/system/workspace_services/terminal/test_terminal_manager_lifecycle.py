"""管理器退出和重启保留PTY 事实；测试自行回收其登记的进程组。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from tests.e2e.system.workspace_services.terminal.terminal_process_helpers import (
    create_long_running_terminal,
    create_terminal_scenario_workspace,
    prepare_empty_terminal_e2e_workspace,
    process_exists,
    terminal_ports_with_offset,
    terminal_state_record,
    wait_terminal_process_metadata,
    wait_terminal_snapshot,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.processes.workspace import terminate_process


class ProcessSnapshot(NamedTuple):
    process_state: str
    process_group_id: int
    process_session_id: int | str
    process_start_time: int | str


@pytest.fixture(scope="module")
def e2e_workspace_root_path() -> str:
    """生命周期测试只需空工作区，避免复制未使用的默认项目模板。"""
    return prepare_empty_terminal_e2e_workspace(Path(__file__))


@pytest.fixture(scope="module")
def terminal_run_context() -> TestRunContext:
    return TestRunContext.from_test_file(Path(__file__))


def _registered_process_snapshot(record: dict[str, object]) -> ProcessSnapshot | None:
    pid = int(record["os_pid"])
    if sys.platform == "linux":
        path = Path(f"/proc/{pid}/stat")
        if not path.exists():
            return None
        fields = path.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        if len(fields) < 20:
            raise RuntimeError(f"Linux 进程 stat 字段不足: pid={pid}")
        snapshot = ProcessSnapshot(
            fields[0],
            int(fields[2]),
            int(fields[3]),
            int(fields[19]),
        )
    elif os.name == "posix":
        result = subprocess.run(
            [
                "ps",
                "-o",
                "stat=",
                "-o",
                "pgid=",
                "-o",
                "sess=",
                "-o",
                "lstart=",
                "-p",
                str(pid),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            no_matching_process = result.returncode == 1 and not result.stdout.strip()
            if no_matching_process and not process_exists(pid):
                return None
            raise RuntimeError(
                f"读取 POSIX 进程快照失败: pid={pid}, stderr={result.stderr.strip()}"
            )
        fields = result.stdout.split()
        if not fields:
            return None
        if len(fields) < 8:
            raise RuntimeError(
                f"POSIX ps 进程快照字段不足: pid={pid}, output={result.stdout!r}"
            )
        snapshot = ProcessSnapshot(
            fields[0][0],
            int(fields[1]),
            fields[2],
            " ".join(fields[3:]),
        )
    else:
        raise RuntimeError(f"不支持读取进程组快照的平台: {sys.platform}")

    for actual, expected, field in (
        (snapshot.process_group_id, record["process_group_id"], "process_group_id"),
        (
            snapshot.process_session_id,
            record["process_session_id"],
            "process_session_id",
        ),
        (
            snapshot.process_start_time,
            record["process_start_time"],
            "process_start_time",
        ),
    ):
        if actual != expected:
            raise AssertionError(
                f"进程身份不匹配: pid={pid}, field={field}, expected={expected!r}, actual={actual!r}"
            )
    return snapshot


def _reap_registered_process(record: dict[str, object]) -> None:
    snapshot = _registered_process_snapshot(record)
    if snapshot is not None and snapshot.process_state not in {"Z", "X", "x"}:
        os.killpg(int(record["process_group_id"]), signal.SIGKILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = _registered_process_snapshot(record)
        if snapshot is None or snapshot.process_state in {"Z", "X", "x"}:
            return
        time.sleep(0.05)
    raise AssertionError(f"测试登记的进程组仍在执行: {record}")


def _assert_preserved_transport(
    root: str, original: dict[str, object], workspace_id: str
) -> dict[str, object]:
    state = json.loads(
        (Path(root) / ".boxteam/terminal-manager/terminals.json").read_text()
    )
    assert state["schema_version"] == 3 and state["workspace_id"] == workspace_id
    record = terminal_state_record(root, str(original["terminal_id"]))
    assert not {
        "session_id",
        "source_thread_id",
        "retention_scope",
        "thread_associations",
        "owner_revision",
    }.intersection(record)
    for field in (
        "terminal_id",
        "workspace_id",
        "os_pid",
        "process_group_id",
        "process_session_id",
        "process_start_time",
        "cwd_relative",
        "command",
        "args",
    ):
        assert record[field] == original[field]
    assert record["transport_owner_revision"] == original["transport_owner_revision"]
    assert record["reconciliation_status"] == "reconcile_required"
    assert record["status"] not in {"terminated", "deleted"}
    assert record["last_command_status"] == original["last_command_status"]
    return record


@pytest.mark.skipif(os.name != "posix", reason="该用例核验 POSIX 持久进程组")
def test_terminal_manager_sigterm_preserves_registered_owner_for_reconciliation(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
    terminal_run_context: TestRunContext,
):
    backend_port, _frontend_port = terminal_ports_with_offset(e2e_backend_port, 70)
    root = create_terminal_scenario_workspace(e2e_workspace_root_path, "sigterm")
    workspace_id = load_or_create_workspace_id(Path(root))
    process = start_terminal_backend(
        backend_port=backend_port,
        workspace_root=root,
        artifacts_dir=terminal_run_context.artifacts_dir,
    )
    original = None
    try:
        terminal = create_long_running_terminal(
            backend_port=backend_port, workspace_root=root, title="sigterm preserve"
        )
        terminal_id = str(terminal["terminal_id"])
        wait_terminal_process_metadata(backend_port, terminal_id, workspace_id)
        original = terminal_state_record(root, terminal_id)
        process.terminate()
        assert process.wait(timeout=10) == 128 + signal.SIGTERM
        record = _assert_preserved_transport(root, original, workspace_id)
        assert record["release_reason"] == "terminal_manager_sigterm"
        assert _registered_process_snapshot(original) is not None
    finally:
        terminate_process(process)
        if original is not None:
            _reap_registered_process(original)


@pytest.mark.skipif(os.name != "posix", reason="该用例核验 POSIX 持久进程组")
def test_terminal_manager_restart_preserves_identity_without_claiming_pty_recovered(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
    terminal_run_context: TestRunContext,
):
    backend_port, _frontend_port = terminal_ports_with_offset(e2e_backend_port, 72)
    root = create_terminal_scenario_workspace(e2e_workspace_root_path, "startup")
    workspace_id = load_or_create_workspace_id(Path(root))
    process = start_terminal_backend(
        backend_port=backend_port,
        workspace_root=root,
        artifacts_dir=terminal_run_context.artifacts_dir,
    )
    original = None
    try:
        terminal = create_long_running_terminal(
            backend_port=backend_port, workspace_root=root, title="restart preserve"
        )
        terminal_id = str(terminal["terminal_id"])
        wait_terminal_process_metadata(backend_port, terminal_id, workspace_id)
        original = terminal_state_record(root, terminal_id)
        process.kill()
        process.wait(timeout=10)
        process = start_terminal_backend(
            backend_port=backend_port,
            workspace_root=root,
            artifacts_dir=terminal_run_context.artifacts_dir,
        )
        snapshot = wait_terminal_snapshot(backend_port, terminal_id, workspace_id)
        record = _assert_preserved_transport(root, original, workspace_id)
        assert snapshot["terminal_id"] == terminal_id
        assert snapshot["reconciliation_status"] == "reconcile_required"
        assert snapshot["pty_worker_pid"] is None
        assert record["last_input"] == original["last_input"]
        assert record["sequence"] == original["sequence"]
    finally:
        terminate_process(process)
        if original is not None:
            _reap_registered_process(original)
