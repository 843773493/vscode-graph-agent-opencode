from __future__ import annotations

import os

import pytest

from tests.e2e.terminal.terminal_process_helpers import (
    create_long_running_terminal,
    get_terminal_snapshot,
    kill_process_on_port,
    lifecycle_workspace,
    process_exists,
    start_terminal_backend,
    terminal_ports_with_offset,
    terminal_state_record,
    terminate_process,
    wait_process_gone,
    wait_terminal_process_metadata,
    wait_terminal_snapshot,
)


@pytest.mark.skipif(os.name == "nt", reason="进程 session 清理测试仅覆盖当前 Linux 运行环境")
def test_terminal_manager_sigterm_releases_running_terminal_process(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> None:
    backend_port, frontend_port = terminal_ports_with_offset(e2e_backend_port, 70)
    workspace_root = lifecycle_workspace(e2e_workspace_root_path, "sigterm")
    kill_process_on_port(backend_port)
    process = start_terminal_backend(
        backend_port=backend_port,
        frontend_port=frontend_port,
        workspace_root=workspace_root,
    )
    try:
        terminal = create_long_running_terminal(
            backend_port=backend_port,
            workspace_root=workspace_root,
            session_id="sigterm_release_session",
        )
        terminal_id = str(terminal["terminal_id"])
        os_pid = int(terminal["os_pid"])
        wait_terminal_process_metadata(backend_port, terminal_id)
        assert process_exists(os_pid)

        process.terminate()
        process.wait(timeout=10)

        wait_process_gone(os_pid)
        record = terminal_state_record(workspace_root, terminal_id)
        assert record["status"] == "terminated"
        assert record["release_reason"] == "terminal_manager_sigterm"
        assert record["last_command_status"] in {None, "terminated"}
    finally:
        terminate_process(process)
        kill_process_on_port(backend_port)


@pytest.mark.skipif(os.name == "nt", reason="进程 session 清理测试仅覆盖当前 Linux 运行环境")
def test_terminal_manager_startup_reaper_releases_previous_running_terminal(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> None:
    backend_port, frontend_port = terminal_ports_with_offset(e2e_backend_port, 72)
    workspace_root = lifecycle_workspace(e2e_workspace_root_path, "startup")
    kill_process_on_port(backend_port)
    process = start_terminal_backend(
        backend_port=backend_port,
        frontend_port=frontend_port,
        workspace_root=workspace_root,
    )
    try:
        terminal = create_long_running_terminal(
            backend_port=backend_port,
            workspace_root=workspace_root,
            session_id="startup_reaper_session",
        )
        terminal_id = str(terminal["terminal_id"])
        os_pid = int(terminal["os_pid"])
        wait_terminal_process_metadata(backend_port, terminal_id)
        assert process_exists(os_pid)

        process.kill()
        process.wait(timeout=10)

        process = start_terminal_backend(
            backend_port=backend_port,
            frontend_port=frontend_port,
            workspace_root=workspace_root,
        )

        snapshot = wait_terminal_snapshot(backend_port, terminal_id)
        assert snapshot["status"] == "terminated"
        assert snapshot["release_reason"] == "terminal_manager_startup_cleanup"
        wait_process_gone(os_pid)
    finally:
        terminate_process(process)
        kill_process_on_port(backend_port)
