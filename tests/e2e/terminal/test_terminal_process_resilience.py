from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path

import httpx
import pytest
import websockets

from tests.e2e.terminal.terminal_process_helpers import (
    kill_process_on_port,
    lifecycle_workspace,
    process_exists,
    start_terminal_backend,
    terminal_ports_with_offset,
    terminate_process,
    wait_process_gone,
)


def _direct_child_pids(pid: int) -> set[int]:
    children_path = Path(f"/proc/{pid}/task/{pid}/children")
    if not children_path.exists():
        return set()
    return {
        int(value)
        for value in children_path.read_text(encoding="utf-8").split()
        if value
    }


async def _wait_for_output(
    websocket: websockets.ClientConnection,
    text: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(
            websocket.recv(),
            timeout=max(deadline - time.monotonic(), 0.1),
        )
        message = json.loads(raw)
        if message.get("type") == "output" and text in str(message.get("data", "")):
            return message
    raise TimeoutError(f"未收到终端输出: {text}")


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="PTY Worker 进程清理断言仅覆盖当前 Linux 环境")
async def test_pty_worker_crash_is_isolated_and_shell_is_reaped(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> None:
    backend_port, frontend_port = terminal_ports_with_offset(e2e_backend_port, 74)
    workspace_root = lifecycle_workspace(e2e_workspace_root_path, "worker_isolation")
    kill_process_on_port(backend_port)
    process = start_terminal_backend(
        backend_port=backend_port,
        frontend_port=frontend_port,
        workspace_root=workspace_root,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}",
            timeout=5,
        ) as client:
            response = await client.post(
                "/api/terminals",
                json={
                    "session_id": "worker_isolation",
                    "title": "worker isolation",
                    "cwd": str(Path(workspace_root).resolve()),
                },
            )
            response.raise_for_status()
            terminal = response.json()["data"]
            terminal_id = str(terminal["terminal_id"])
            shell_pid = int(terminal["os_pid"])
            worker_pid = int(terminal["pty_worker_pid"])
            assert worker_pid not in {process.pid, shell_pid}
            survivor_response = await client.post(
                "/api/terminals",
                json={
                    "session_id": "worker_isolation_survivor",
                    "title": "worker isolation survivor",
                    "cwd": str(Path(workspace_root).resolve()),
                },
            )
            survivor_response.raise_for_status()
            survivor_id = str(survivor_response.json()["data"]["terminal_id"])

            os.kill(worker_pid, 9)
            deadline = time.monotonic() + 5
            snapshot: dict[str, object] | None = None
            while time.monotonic() < deadline:
                candidate = (
                    await client.get(f"/api/terminals/{terminal_id}")
                ).json()["data"]
                if candidate["status"] == "exited":
                    snapshot = candidate
                    break
                await asyncio.sleep(0.05)

            assert snapshot is not None
            assert snapshot["release_reason"] == "pty_worker_exit"
            wait_process_gone(shell_pid)
            assert not process_exists(worker_pid)

            health = await client.get("/health")
            health.raise_for_status()
            assert health.json()["ok"] is True
            survivor = (await client.get(f"/api/terminals/{survivor_id}")).json()["data"]
            assert survivor["status"] == "running"
            (await client.delete(f"/api/terminals/{survivor_id}")).raise_for_status()
    finally:
        terminate_process(process)
        kill_process_on_port(backend_port)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Worker 子进程泄漏检查仅覆盖当前 Linux 环境")
async def test_failed_terminal_start_rolls_back_session_and_worker(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> None:
    backend_port, frontend_port = terminal_ports_with_offset(e2e_backend_port, 78)
    workspace_root = lifecycle_workspace(e2e_workspace_root_path, "failed_start")
    kill_process_on_port(backend_port)
    process = start_terminal_backend(
        backend_port=backend_port,
        frontend_port=frontend_port,
        workspace_root=workspace_root,
    )
    try:
        initial_children = _direct_child_pids(process.pid)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}",
            timeout=5,
        ) as client:
            response = await client.post(
                "/api/terminals",
                json={
                    "session_id": "failed_start",
                    "title": "failed start",
                    "cwd": str(Path(workspace_root).resolve()),
                    "command": "/definitely/missing/boxteam-shell",
                    "args": [],
                },
            )
            assert response.status_code == 500
            assert "隔离 PTY Worker 错误" in response.json()["error"]

            terminals = (await client.get("/api/terminals")).json()["data"]
            assert terminals == []
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if _direct_child_pids(process.pid) == initial_children:
                    break
                await asyncio.sleep(0.05)
            assert _direct_child_pids(process.pid) == initial_children
            (await client.get("/health")).raise_for_status()
    finally:
        terminate_process(process)
        kill_process_on_port(backend_port)


@pytest.mark.asyncio
async def test_websocket_attach_messages_are_processed_in_order(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> None:
    backend_port, frontend_port = terminal_ports_with_offset(e2e_backend_port, 80)
    workspace_root = lifecycle_workspace(e2e_workspace_root_path, "ordered_attach")
    kill_process_on_port(backend_port)
    process = start_terminal_backend(
        backend_port=backend_port,
        frontend_port=frontend_port,
        workspace_root=workspace_root,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}",
            timeout=5,
        ) as client:
            terminal_ids = []
            for title in ("attach A", "attach B"):
                response = await client.post(
                    "/api/terminals",
                    json={
                        "session_id": "ordered_attach",
                        "title": title,
                        "cwd": str(Path(workspace_root).resolve()),
                    },
                )
                response.raise_for_status()
                terminal_ids.append(str(response.json()["data"]["terminal_id"]))

            async with websockets.connect(
                f"ws://127.0.0.1:{backend_port}/terminal"
            ) as websocket:
                for terminal_id in terminal_ids:
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "attach",
                                "terminalId": terminal_id,
                                "cols": 80,
                                "rows": 24,
                            }
                        )
                    )
                deadline = time.monotonic() + 5
                attached_ids = []
                while time.monotonic() < deadline and len(attached_ids) < 2:
                    message = json.loads(
                        await asyncio.wait_for(websocket.recv(), timeout=1)
                    )
                    if message.get("type") == "attached":
                        attached_ids.append(str(message["terminalId"]))
                assert attached_ids == terminal_ids

                first = (await client.get(f"/api/terminals/{terminal_ids[0]}")).json()[
                    "data"
                ]
                second = (
                    await client.get(f"/api/terminals/{terminal_ids[1]}")
                ).json()["data"]
                assert first["client_count"] == 0
                assert second["client_count"] == 1

            for terminal_id in terminal_ids:
                (await client.delete(f"/api/terminals/{terminal_id}")).raise_for_status()
    finally:
        terminate_process(process)
        kill_process_on_port(backend_port)


@pytest.mark.asyncio
async def test_websocket_reconnect_replays_only_unacknowledged_output(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> None:
    backend_port, frontend_port = terminal_ports_with_offset(e2e_backend_port, 76)
    workspace_root = lifecycle_workspace(e2e_workspace_root_path, "websocket_reconnect")
    kill_process_on_port(backend_port)
    process = start_terminal_backend(
        backend_port=backend_port,
        frontend_port=frontend_port,
        workspace_root=workspace_root,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}",
            timeout=5,
        ) as client:
            response = await client.post(
                "/api/terminals",
                json={
                    "session_id": "websocket_reconnect",
                    "title": "websocket reconnect",
                    "cwd": str(Path(workspace_root).resolve()),
                },
            )
            response.raise_for_status()
            terminal = response.json()["data"]
            terminal_id = str(terminal["terminal_id"])
            initial_sequence = int(terminal["sequence"])
            websocket_url = f"ws://127.0.0.1:{backend_port}/terminal"

            async with websockets.connect(websocket_url) as websocket:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "attach",
                            "terminalId": terminal_id,
                            "afterSequence": initial_sequence,
                        }
                    )
                )
                attached = json.loads(await websocket.recv())
                assert attached["type"] == "attached"
                assert attached["replayMode"] == "incremental"

                await websocket.send(
                    json.dumps({"type": "input", "data": "printf 'RECONNECT_ONE\\n'\r"})
                )
                first_output = await _wait_for_output(websocket, "RECONNECT_ONE")
                acknowledged_sequence = int(first_output["sequence"])
                await websocket.send(
                    json.dumps(
                        {"type": "ack", "sequence": acknowledged_sequence}
                    )
                )

            write_response = await client.post(
                f"/api/terminals/{terminal_id}/write",
                json={"data": "printf 'RECONNECT_TWO\\n'\r", "source": "agent"},
            )
            write_response.raise_for_status()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                snapshot = (
                    await client.get(f"/api/terminals/{terminal_id}")
                ).json()["data"]
                if "RECONNECT_TWO" in str(snapshot["buffer"]):
                    break
                await asyncio.sleep(0.05)
            else:
                raise TimeoutError("断线期间的终端输出未写入缓冲")

            async with websockets.connect(websocket_url) as websocket:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "attach",
                            "terminalId": terminal_id,
                            "afterSequence": acknowledged_sequence,
                        }
                    )
                )
                attached = json.loads(await websocket.recv())
                assert attached["type"] == "attached"
                assert attached["replayMode"] == "incremental"
                replayed = await _wait_for_output(websocket, "RECONNECT_TWO")
                assert int(replayed["sequence"]) > acknowledged_sequence

            delete_response = await client.delete(f"/api/terminals/{terminal_id}")
            delete_response.raise_for_status()
    finally:
        terminate_process(process)
        kill_process_on_port(backend_port)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="孤儿进程树清理断言仅覆盖当前 Linux 环境")
async def test_manager_sigkill_reaps_terminal_and_term_resistant_descendant(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> None:
    backend_port, frontend_port = terminal_ports_with_offset(e2e_backend_port, 82)
    workspace_root = lifecycle_workspace(e2e_workspace_root_path, "manager_sigkill")
    kill_process_on_port(backend_port)
    process = start_terminal_backend(
        backend_port=backend_port,
        frontend_port=frontend_port,
        workspace_root=workspace_root,
    )
    worker_pid = 0
    shell_pid = 0
    descendant_pid = 0
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}",
            timeout=5,
        ) as client:
            response = await client.post(
                "/api/terminals",
                json={
                    "session_id": "manager_sigkill",
                    "title": "manager sigkill",
                    "cwd": str(Path(workspace_root).resolve()),
                },
            )
            response.raise_for_status()
            terminal = response.json()["data"]
            terminal_id = str(terminal["terminal_id"])
            worker_pid = int(terminal["pty_worker_pid"])
            shell_pid = int(terminal["os_pid"])

            command = (
                "sh -c 'trap \"\" TERM HUP INT; while :; do sleep 1; done' "
                "& printf '__CHILD_PID__:%s\\n' \"$!\"\r"
            )
            write_response = await client.post(
                f"/api/terminals/{terminal_id}/write",
                json={"data": command, "source": "agent"},
            )
            write_response.raise_for_status()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                snapshot = (
                    await client.get(f"/api/terminals/{terminal_id}")
                ).json()["data"]
                marker = "__CHILD_PID__:"
                buffer = str(snapshot["buffer"])
                marker_index = buffer.rfind(marker)
                if marker_index >= 0:
                    pid_text = buffer[marker_index + len(marker) :].splitlines()[0]
                    if pid_text.strip().isdigit():
                        descendant_pid = int(pid_text.strip())
                        break
                await asyncio.sleep(0.05)
            assert descendant_pid > 0
            assert process_exists(descendant_pid)

        process.kill()
        process.wait(timeout=5)
        wait_process_gone(worker_pid)
        wait_process_gone(shell_pid)
        wait_process_gone(descendant_pid)
    finally:
        terminate_process(process)
        kill_process_on_port(backend_port)


@pytest.mark.asyncio
async def test_concurrent_kill_and_delete_share_one_release(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> None:
    backend_port, frontend_port = terminal_ports_with_offset(e2e_backend_port, 84)
    workspace_root = lifecycle_workspace(e2e_workspace_root_path, "concurrent_release")
    kill_process_on_port(backend_port)
    process = start_terminal_backend(
        backend_port=backend_port,
        frontend_port=frontend_port,
        workspace_root=workspace_root,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}",
            timeout=10,
        ) as client:
            response = await client.post(
                "/api/terminals",
                json={
                    "session_id": "concurrent_release",
                    "title": "concurrent release",
                    "cwd": str(Path(workspace_root).resolve()),
                },
            )
            response.raise_for_status()
            terminal = response.json()["data"]
            terminal_id = str(terminal["terminal_id"])
            shell_pid = int(terminal["os_pid"])

            kill_response, delete_response = await asyncio.gather(
                client.post(f"/api/terminals/{terminal_id}/kill"),
                client.delete(f"/api/terminals/{terminal_id}"),
            )
            kill_response.raise_for_status()
            delete_response.raise_for_status()
            snapshot = (
                await client.get(f"/api/terminals/{terminal_id}")
            ).json()["data"]
            assert snapshot["status"] == "deleted"
            wait_process_gone(shell_pid)
            (await client.get("/health")).raise_for_status()
    finally:
        terminate_process(process)
        kill_process_on_port(backend_port)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="PTY session 后代清理断言仅覆盖当前 Linux 环境")
async def test_normal_shell_exit_reaps_term_resistant_descendant(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> None:
    backend_port, frontend_port = terminal_ports_with_offset(e2e_backend_port, 86)
    workspace_root = lifecycle_workspace(e2e_workspace_root_path, "normal_exit_cleanup")
    kill_process_on_port(backend_port)
    process = start_terminal_backend(
        backend_port=backend_port,
        frontend_port=frontend_port,
        workspace_root=workspace_root,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}",
            timeout=10,
        ) as client:
            response = await client.post(
                "/api/terminals",
                json={
                    "session_id": "normal_exit_cleanup",
                    "title": "normal exit cleanup",
                    "cwd": str(Path(workspace_root).resolve()),
                },
            )
            response.raise_for_status()
            terminal = response.json()["data"]
            terminal_id = str(terminal["terminal_id"])
            worker_pid = int(terminal["pty_worker_pid"])
            command = (
                "sh -c 'trap \"\" TERM HUP INT; while :; do sleep 1; done' "
                "& printf '__CHILD_PID__:%s\\n' \"$!\"; exit\r"
            )
            (await client.post(
                f"/api/terminals/{terminal_id}/write",
                json={"data": command, "source": "agent"},
            )).raise_for_status()

            descendant_pid = 0
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                snapshot = (
                    await client.get(f"/api/terminals/{terminal_id}")
                ).json()["data"]
                marker = "__CHILD_PID__:"
                buffer = str(snapshot["buffer"])
                marker_index = buffer.rfind(marker)
                if marker_index >= 0:
                    pid_text = buffer[marker_index + len(marker) :].splitlines()[0]
                    if pid_text.strip().isdigit():
                        descendant_pid = int(pid_text.strip())
                if snapshot["status"] == "exited" and descendant_pid > 0:
                    break
                await asyncio.sleep(0.05)
            assert snapshot["status"] == "exited"
            assert descendant_pid > 0
            wait_process_gone(descendant_pid)
            wait_process_gone(worker_pid)
    finally:
        terminate_process(process)
        kill_process_on_port(backend_port)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="PTY ready 前后代清理断言仅覆盖当前 Linux 环境")
async def test_pre_ready_exit_reaps_term_resistant_descendant(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> None:
    backend_port, frontend_port = terminal_ports_with_offset(e2e_backend_port, 88)
    workspace_root = lifecycle_workspace(e2e_workspace_root_path, "pre_ready_cleanup")
    child_pid_file = Path(workspace_root) / "pre-ready-child.pid"
    kill_process_on_port(backend_port)
    process = start_terminal_backend(
        backend_port=backend_port,
        frontend_port=frontend_port,
        workspace_root=workspace_root,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}",
            timeout=10,
        ) as client:
            command = (
                "sh -c 'trap \"\" TERM HUP INT; while :; do sleep 1; done' & "
                f"printf '%s' \"$!\" > {shlex.quote(str(child_pid_file))}; exit 0"
            )
            response = await client.post(
                "/api/terminals",
                json={
                    "session_id": "pre_ready_cleanup",
                    "title": "pre-ready cleanup",
                    "cwd": str(Path(workspace_root).resolve()),
                    "command": "/bin/sh",
                    "args": ["-c", command],
                },
            )
            assert response.status_code == 500
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not child_pid_file.exists():
                await asyncio.sleep(0.05)
            descendant_pid = int(child_pid_file.read_text(encoding="utf-8"))
            wait_process_gone(descendant_pid)
            assert (await client.get("/api/terminals")).json()["data"] == []
    finally:
        terminate_process(process)
        kill_process_on_port(backend_port)


@pytest.mark.asyncio
async def test_shutdown_persist_failure_still_exits_manager(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> None:
    backend_port, frontend_port = terminal_ports_with_offset(e2e_backend_port, 90)
    workspace_root = lifecycle_workspace(e2e_workspace_root_path, "shutdown_failure")
    kill_process_on_port(backend_port)
    process = start_terminal_backend(
        backend_port=backend_port,
        frontend_port=frontend_port,
        workspace_root=workspace_root,
    )
    manager_path = Path(workspace_root) / ".boxteam" / "terminal-manager"
    backup_path = Path(workspace_root) / ".boxteam" / "terminal-manager-backup"
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}",
            timeout=5,
        ) as client:
            response = await client.post(
                "/api/terminals",
                json={
                    "session_id": "shutdown_failure",
                    "title": "shutdown failure",
                    "cwd": str(Path(workspace_root).resolve()),
                },
            )
            response.raise_for_status()

        manager_path.rename(backup_path)
        manager_path.write_text("阻止状态目录写入", encoding="utf-8")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) != 0
    finally:
        if manager_path.is_file():
            manager_path.unlink()
        if backup_path.exists():
            backup_path.rename(manager_path)
        terminate_process(process)
        kill_process_on_port(backend_port)
