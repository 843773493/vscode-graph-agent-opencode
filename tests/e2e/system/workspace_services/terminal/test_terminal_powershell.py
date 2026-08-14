from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

import httpx
import pytest
import websockets

from app.agents.tools.terminal import create_exec_command_tool
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from tests.support.processes import kill_process_on_port, terminate_process


@dataclass(frozen=True, slots=True)
class WindowsTerminalBackend:
    port: int
    frontend_port: int
    frontend_url: str
    workspace_root: str
    powershell: str
    workspace_id: str


def _wait_for_health(
    *,
    url: str,
    process: subprocess.Popen[str],
    stdout_path: Path,
    stderr_path: Path,
) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "Windows 终端后端提前退出: "
                f"returncode={process.returncode}, "
                f"stdout={stdout_path}, stderr={stderr_path}"
            )
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"Windows 终端服务未就绪: url={url}")


@pytest.fixture(scope="module")
def windows_terminal_backend(
    e2e_workspace_root_path: str,
    e2e_backend_port: int,
) -> Generator[WindowsTerminalBackend, None, None]:
    if os.name != "nt":
        raise RuntimeError("该测试必须在真实 Windows 客体中运行")

    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Windows 客体未找到 node")
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        raise RuntimeError("Windows 客体未找到 powershell.exe")

    port = e2e_backend_port + 40
    frontend_port = port + 1
    frontend_url = f"http://127.0.0.1:{frontend_port}"
    kill_process_on_port(port)
    kill_process_on_port(frontend_port)
    workspace_root = str(Path(e2e_workspace_root_path).resolve())
    project_root = Path.cwd().resolve()
    server_root = project_root / "src" / "workspace-services" / "terminal" / "server"
    log_dir = Path(workspace_root) / ".boxteam" / "windows-terminal"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "terminal-backend.stdout.log"
    stderr_path = log_dir / "terminal-backend.stderr.log"
    frontend_stdout_path = log_dir / "terminal-frontend.stdout.log"
    frontend_stderr_path = log_dir / "terminal-frontend.stderr.log"
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("w", encoding="utf-8")
    frontend_stdout_file = frontend_stdout_path.open("w", encoding="utf-8")
    frontend_stderr_file = frontend_stderr_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env["WORKSPACE_ROOT"] = workspace_root
    env["BOXTEAM_TERMINAL_WORKSPACE_ROOT"] = workspace_root

    process = subprocess.Popen(
        [
            node,
            "backend.js",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workspace-root",
            workspace_root,
            "--frontend-url",
            frontend_url,
        ],
        cwd=server_root,
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
        text=True,
    )
    frontend_process = subprocess.Popen(
        [
            node,
            "server.js",
            "--host",
            "127.0.0.1",
            "--port",
            str(frontend_port),
            "--backend-url",
            f"http://127.0.0.1:{port}",
            "--workspace-root",
            workspace_root,
            "--asset-root",
            str(project_root),
        ],
        cwd=project_root / "src" / "workspace-services" / "terminal" / "client",
        env=env,
        stdout=frontend_stdout_file,
        stderr=frontend_stderr_file,
        text=True,
    )
    try:
        _wait_for_health(
            url=f"http://127.0.0.1:{port}/health",
            process=process,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        _wait_for_health(
            url=f"{frontend_url}/health",
            process=frontend_process,
            stdout_path=frontend_stdout_path,
            stderr_path=frontend_stderr_path,
        )
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            health = json.loads(response.read().decode("utf-8"))
        workspace_id = health.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise RuntimeError(f"Windows 终端后端健康响应缺少 workspace_id: {health}")
        yield WindowsTerminalBackend(
            port=port,
            frontend_port=frontend_port,
            frontend_url=frontend_url,
            workspace_root=workspace_root,
            powershell=powershell,
            workspace_id=workspace_id,
        )
    finally:
        terminate_process(frontend_process)
        terminate_process(process)
        kill_process_on_port(frontend_port)
        kill_process_on_port(port)
        stdout_file.close()
        stderr_file.close()
        frontend_stdout_file.close()
        frontend_stderr_file.close()


async def _create_terminal(
    client: httpx.AsyncClient,
    backend: WindowsTerminalBackend,
    session_id: str,
) -> str:
    response = await client.post(
        "/api/terminals",
        json={
            "session_id": session_id,
            "title": "Windows PowerShell E2E",
            "cwd": backend.workspace_root,
            "command": backend.powershell,
            "args": ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass"],
        },
    )
    response.raise_for_status()
    terminal_id = response.json()["data"]["terminal_id"]
    assert isinstance(terminal_id, str) and terminal_id
    return terminal_id


async def _snapshot(
    client: httpx.AsyncClient,
    terminal_id: str,
) -> dict[str, object]:
    response = await client.get(f"/api/terminals/{terminal_id}")
    response.raise_for_status()
    snapshot = response.json()["data"]
    assert isinstance(snapshot, dict)
    return snapshot


async def _wait_snapshot(
    client: httpx.AsyncClient,
    terminal_id: str,
    predicate: Callable[[dict[str, object]], bool],
    *,
    timeout_seconds: float = 20,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = await _snapshot(client, terminal_id)
        if predicate(latest):
            return latest
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"Windows 终端状态未达到预期: terminal_id={terminal_id}, snapshot={latest}"
    )


async def _write(
    client: httpx.AsyncClient,
    terminal_id: str,
    data: str,
) -> dict[str, object]:
    response = await client.post(
        f"/api/terminals/{terminal_id}/write",
        json={"data": data, "source": "user"},
    )
    response.raise_for_status()
    result = response.json()["data"]
    assert isinstance(result, dict)
    return result


async def _receive_type(websocket, expected_type: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(websocket.recv(), timeout=1)
        message = json.loads(raw)
        if message.get("type") == expected_type:
            return message
    raise AssertionError(f"Windows 终端 WebSocket 未收到消息: {expected_type}")


@pytest.mark.asyncio
async def test_powershell_executes_unicode_crlf_and_exit_code(
    windows_terminal_backend: WindowsTerminalBackend,
) -> None:
    backend = windows_terminal_backend
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{backend.port}",
        timeout=10,
    ) as client:
        terminal_id = await _create_terminal(client, backend, "powershell_exec")
        await _wait_snapshot(
            client,
            terminal_id,
            lambda snapshot: snapshot.get("status") == "running",
        )
        await _write(
            client,
            terminal_id,
            "Write-Output 'BOXTEAM_PS_中文'; Write-Output 'BOXTEAM_PS_CRLF'; exit 7\r\n",
        )
        snapshot = await _wait_snapshot(
            client,
            terminal_id,
            lambda current: current.get("status") in {"exited", "terminated"},
        )

        raw_buffer = str(snapshot.get("buffer", ""))
        display_buffer = str(snapshot.get("display_buffer", ""))
        frontend_response = await client.get(
            f"{backend.frontend_url}/?terminalId={terminal_id}"
        )
        frontend_response.raise_for_status()
        assert "持久终端" in frontend_response.text
        assert "BOXTEAM_PS_中文" in display_buffer, repr(raw_buffer)
        assert "BOXTEAM_PS_CRLF" in display_buffer, repr(raw_buffer)
        assert "\r" in raw_buffer, repr(raw_buffer)
        assert snapshot.get("exit_code") == 7, snapshot

        delete_response = await client.delete(f"/api/terminals/{terminal_id}")
        delete_response.raise_for_status()


@pytest.mark.asyncio
async def test_powershell_websocket_input_kill_and_delete(
    windows_terminal_backend: WindowsTerminalBackend,
) -> None:
    backend = windows_terminal_backend
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{backend.port}",
        timeout=10,
    ) as client:
        terminal_id = await _create_terminal(client, backend, "powershell_interactive")
        websocket_url = f"ws://127.0.0.1:{backend.port}/terminal"
        async with websockets.connect(websocket_url) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "attach",
                        "terminalId": terminal_id,
                        "cols": 120,
                        "rows": 30,
                    }
                )
            )
            attached = await _receive_type(websocket, "attached")
            assert attached["snapshot"]["terminal_id"] == terminal_id

            await websocket.send(
                json.dumps(
                    {
                        "type": "input",
                        "data": (
                            "Write-Output 'BOXTEAM_PS_READY'; "
                            "$value = Read-Host 'value'; "
                            "Write-Output ('BOXTEAM_PS_INPUT:' + $value); "
                            "Start-Sleep -Seconds 30\r\n"
                        ),
                    }
                )
            )
            await _receive_type(websocket, "input")
            await _wait_snapshot(
                client,
                terminal_id,
                lambda snapshot: "BOXTEAM_PS_READY" in str(snapshot.get("display_buffer", "")),
            )

            await websocket.send(
                json.dumps({"type": "input", "data": "BOXTEAM_INPUT_中文\r\n"})
            )
            await _receive_type(websocket, "input")
            snapshot = await _wait_snapshot(
                client,
                terminal_id,
                lambda current: "BOXTEAM_PS_INPUT:BOXTEAM_INPUT_中文"
                in str(current.get("display_buffer", "")),
            )
            assert snapshot.get("status") == "running", snapshot

        kill_response = await client.post(
            f"/api/terminals/{terminal_id}/kill",
            json={"reason": "model_requested"},
        )
        kill_response.raise_for_status()
        killed = kill_response.json()["data"]["terminal"]
        assert killed["status"] == "terminated"
        assert killed["release_reason"] == "model_requested"

        delete_response = await client.delete(f"/api/terminals/{terminal_id}")
        delete_response.raise_for_status()
        missing_response = await client.get(
            f"/api/terminals/{terminal_id}?missing_as_deleted=1"
        )
        missing_response.raise_for_status()
        assert missing_response.json()["data"]["status"] == "deleted"


@pytest.mark.asyncio
async def test_agent_exec_command_uses_windows_cmd_and_powershell_wrappers(
    windows_terminal_backend: WindowsTerminalBackend,
) -> None:
    backend = windows_terminal_backend
    client = TerminalManagerClient(
        backend_url=f"http://127.0.0.1:{backend.port}",
        state_file=(
            Path(backend.workspace_root)
            / ".boxteam"
            / "terminal-manager"
            / "terminals.json"
        ),
        workspace_id=backend.workspace_id,
    )
    tool = create_exec_command_tool(
        session_id="windows_agent_exec_command",
        terminal_client=client,
    )

    cmd_result = await tool.ainvoke(
        {
            "cmd": "echo BOXTEAM_AGENT_CMD_中文 & cmd.exe /c exit 7",
            "workdir": backend.workspace_root,
        }
    )
    assert cmd_result["exit_code"] == 7
    assert "BOXTEAM_AGENT_CMD_中文" in cmd_result["output"]

    powershell_result = await tool.ainvoke(
        {
            "cmd": 'Write-Output "BOXTEAM_AGENT_PS_中文"; cmd.exe /c exit 9',
            "workdir": backend.workspace_root,
            "shell": backend.powershell,
        }
    )
    assert powershell_result["exit_code"] == 9
    assert "BOXTEAM_AGENT_PS_中文" in powershell_result["output"]
