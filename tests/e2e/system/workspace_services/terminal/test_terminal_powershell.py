from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Generator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

import httpx
import pytest
import websockets

from app.agents.tools.terminal import create_exec_command_tool
from app.core.identifier import create_uuid_hex
from app.core.mounted_workspaces import MountedWorkspace
from app.core.workspace_identity import load_or_create_workspace_id
from tests.e2e.system.workspace_services.terminal.terminal_process_helpers import (
    prepare_empty_terminal_e2e_workspace,
)
from tests.harness.python.terminal_plugin import TerminalPluginGateway
from tests.support.workspace_tool_runtime import workspace_tool_runtime


@pytest.fixture(scope="module")
def e2e_workspace_root_path() -> str:
    return prepare_empty_terminal_e2e_workspace(Path(__file__))


def _terminal_api_url(gateway: TerminalPluginGateway, suffix: str) -> str:
    return (
        f"{gateway.http_service_url}{suffix}?"
        + urlencode({"package_version": "0.1.0", "workspace_id": gateway.workspace_id})
    )


async def _create_terminal(
    client: httpx.AsyncClient,
    gateway: TerminalPluginGateway,
    title: str,
) -> str:
    response = await client.post(
        _terminal_api_url(gateway, "/api/terminals"),
        json={
            "workspace_id": backend.workspace_id,
            "terminal_id": f"term_{create_uuid_hex()}",
            "owner_revision": 1,
            "title": title,
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
    gateway: TerminalPluginGateway,
    terminal_id: str,
) -> dict[str, object]:
    response = await client.get(_terminal_api_url(gateway, f"/api/terminals/{terminal_id}"))
    response.raise_for_status()
    snapshot = response.json()["data"]
    assert isinstance(snapshot, dict)
    return snapshot


async def _wait_snapshot(
    client: httpx.AsyncClient,
    gateway: TerminalPluginGateway,
    terminal_id: str,
    predicate: Callable[[dict[str, object]], bool],
    *,
    timeout_seconds: float = 20,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = await _snapshot(client, gateway, terminal_id)
        if predicate(latest):
            return latest
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"Windows 终端状态未达到预期: terminal_id={terminal_id}, snapshot={latest}"
    )


async def _write(
    client: httpx.AsyncClient,
    gateway: TerminalPluginGateway,
    terminal_id: str,
    data: str,
) -> dict[str, object]:
    response = await client.post(
        _terminal_api_url(gateway, f"/api/terminals/{terminal_id}/write"),
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
    terminal_plugin_gateway: TerminalPluginGateway,
) -> None:
    gateway = terminal_plugin_gateway
    if not Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists():
        pytest.skip("该场景需要 Windows PowerShell")
    async with httpx.AsyncClient(
        base_url=gateway.gateway_url,
        headers=gateway.workspace_headers,
        timeout=10,
    ) as client:
        terminal_id = await _create_terminal(client, gateway, "powershell_exec")
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
        assert "BOXTEAM_PS_中文" in display_buffer, repr(raw_buffer)
        assert "BOXTEAM_PS_CRLF" in display_buffer, repr(raw_buffer)
        assert "\r" in raw_buffer, repr(raw_buffer)
        assert snapshot.get("exit_code") == 7, snapshot

        delete_response = await client.delete(_terminal_api_url(gateway, f"/api/terminals/{terminal_id}"))
        delete_response.raise_for_status()


@pytest.mark.asyncio
async def test_powershell_websocket_input_kill_and_delete(
    terminal_plugin_gateway: TerminalPluginGateway,
) -> None:
    gateway = terminal_plugin_gateway
    if not Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists():
        pytest.skip("该场景需要 Windows PowerShell")
    async with httpx.AsyncClient(
        base_url=gateway.gateway_url,
        headers=gateway.workspace_headers,
        timeout=10,
    ) as client:
        terminal_id = await _create_terminal(client, gateway, "powershell_interactive")
        grant_response = await client.post(
            _terminal_api_url(gateway, f"/api/terminals/{terminal_id}/transport-grant"),
            json={"owner_revision": 1, "association": None},
        )
        grant_response.raise_for_status()
        grant_token = grant_response.json()["data"]["grant_token"]
        async with websockets.connect(
            gateway.websocket_url,
            additional_headers=gateway.workspace_headers,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "attach",
                        "terminalId": terminal_id,
                        "grantToken": grant_token,
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
                lambda snapshot: (
                    "BOXTEAM_PS_READY" in str(snapshot.get("display_buffer", ""))
                ),
            )

            await websocket.send(
                json.dumps({"type": "input", "data": "BOXTEAM_INPUT_中文\r\n"})
            )
            await _receive_type(websocket, "input")
            snapshot = await _wait_snapshot(
                client,
                terminal_id,
                lambda current: (
                    "BOXTEAM_PS_INPUT:BOXTEAM_INPUT_中文"
                    in str(current.get("display_buffer", ""))
                ),
            )
            assert snapshot.get("status") == "running", snapshot

        kill_response = await client.post(
            _terminal_api_url(gateway, f"/api/terminals/{terminal_id}/kill"),
            json={"reason": "model_requested"},
        )
        kill_response.raise_for_status()
        killed = kill_response.json()["data"]["terminal"]
        assert killed["status"] == "terminated"
        assert killed["release_reason"] == "model_requested"

        delete_response = await client.delete(_terminal_api_url(gateway, f"/api/terminals/{terminal_id}"))
        delete_response.raise_for_status()
        missing_response = await client.get(_terminal_api_url(gateway, f"/api/terminals/{terminal_id}"))
        missing_response.raise_for_status()
        assert missing_response.json()["data"]["status"] == "deleted"


@pytest.mark.asyncio
async def test_agent_exec_command_uses_windows_cmd_and_powershell_wrappers(
    terminal_plugin_gateway: TerminalPluginGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = terminal_plugin_gateway
    if not Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe").exists():
        pytest.skip("该场景需要 Windows PowerShell")
    root = gateway.context.workspace_root
    async with workspace_tool_runtime(root) as (
        container,
        session_id,
        context,
    ):
        tool = create_exec_command_tool(
            session_id=session_id,
            terminal_owner=container.terminal_resource_owner,
            invocation_context=context,
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
