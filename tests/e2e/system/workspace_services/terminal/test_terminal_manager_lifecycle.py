"""管理器退出和重启保留PTY 事实；测试自行回收其登记的进程组。"""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from app.core.workspace_identity import load_or_create_workspace_id
from tests.e2e.system.workspace_services.terminal.terminal_process_helpers import (
    create_long_running_terminal,
    lifecycle_workspace,
    terminal_ports_with_offset,
    terminal_state_record,
    wait_terminal_process_metadata,
    wait_terminal_snapshot,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.processes.workspace import terminate_process


def _registered_process_stat(record: dict[str, object]) -> list[str] | None:
    path = Path(f"/proc/{record['os_pid']}/stat")
    if not path.exists():
        return None
    fields = path.read_text().rsplit(")", 1)[1].split()
    assert int(fields[2]) == record["process_group_id"]
    assert int(fields[3]) == record["process_session_id"]
    assert int(fields[19]) == record["process_start_time"]
    return fields


def _reap_registered_process(record: dict[str, object]) -> None:
    fields = _registered_process_stat(record)
    if fields is not None and fields[0] != "Z":
        os.killpg(int(record["process_group_id"]), signal.SIGKILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        fields = _registered_process_stat(record)
        if fields is None or fields[0] == "Z":
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


@pytest.mark.skipif(os.name == "nt", reason="该用例核验 Linux /proc 与持久进程组")
def test_terminal_manager_sigterm_preserves_registered_owner_for_reconciliation(
    e2e_workspace_root_path: str, e2e_backend_port: int
):
    backend_port, _frontend_port = terminal_ports_with_offset(e2e_backend_port, 70)
    root = lifecycle_workspace(e2e_workspace_root_path, "sigterm")
    workspace_id = load_or_create_workspace_id(Path(root))
    process = start_terminal_backend(backend_port=backend_port, workspace_root=root, artifacts_dir=TestRunContext.from_test_file(Path(__file__)).artifacts_dir)
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
        assert _registered_process_stat(original) is not None
    finally:
        terminate_process(process)
        if original is not None:
            _reap_registered_process(original)


@pytest.mark.skipif(os.name == "nt", reason="该用例核验 Linux /proc 与持久进程组")
def test_terminal_manager_restart_preserves_identity_without_claiming_pty_recovered(
    e2e_workspace_root_path: str, e2e_backend_port: int
):
    backend_port, _frontend_port = terminal_ports_with_offset(e2e_backend_port, 72)
    root = lifecycle_workspace(e2e_workspace_root_path, "startup")
    workspace_id = load_or_create_workspace_id(Path(root))
    process = start_terminal_backend(backend_port=backend_port, workspace_root=root, artifacts_dir=TestRunContext.from_test_file(Path(__file__)).artifacts_dir)
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
        process = start_terminal_backend(backend_port=backend_port, workspace_root=root, artifacts_dir=TestRunContext.from_test_file(Path(__file__)).artifacts_dir)
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
