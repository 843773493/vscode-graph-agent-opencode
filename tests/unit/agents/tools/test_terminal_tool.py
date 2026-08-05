from __future__ import annotations

import base64
import os
import re
from typing import Any

import pytest

from app.agents.tools.terminal import (
    _command_for_shell,
    create_exec_command_tool,
    create_kill_terminal_tool,
    create_list_terminal_sessions_tool,
    create_write_stdin_tool,
)


def test_windows_cmd_wrapper_uses_cmd_syntax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    wrapped = _command_for_shell(
        cmd="echo BOXTEAM_CMD_OK",
        workdir=r"C:\workspace\demo",
        shell=None,
        login=True,
        start_marker="__BOXTEAM_CMD_START_test__",
        done_marker="__BOXTEAM_CMD_DONE_test__",
    )

    assert "${SHELL" not in wrapped
    assert "printf" not in wrapped
    assert 'cd /d "C:\\workspace\\demo"' in wrapped
    assert "echo __BOXTEAM_CMD_START_test__" in wrapped
    assert 'set "__BOXTEAM_RC=%ERRORLEVEL%"' in wrapped
    assert "echo __BOXTEAM_CMD_DONE_test__:%__BOXTEAM_RC%" in wrapped


def test_windows_powershell_wrapper_preserves_command_and_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    wrapped = _command_for_shell(
        cmd='Write-Output "中文"',
        workdir=r"C:\workspace\demo",
        shell=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        login=True,
        start_marker="__BOXTEAM_CMD_START_test__",
        done_marker="__BOXTEAM_CMD_DONE_test__",
    )

    encoded_script = wrapped.rstrip("\r").split()[-1]
    script = base64.b64decode(encoded_script).decode("utf-16le")
    assert "${SHELL" not in wrapped
    assert "-EncodedCommand" in wrapped
    assert "Set-Location -LiteralPath 'C:\\workspace\\demo'" in script
    assert "Write-Output '__BOXTEAM_CMD_START_test__'" in script
    encoded_command = base64.b64encode('Write-Output "中文"'.encode()).decode()
    assert encoded_command in script
    assert "__BOXTEAM_CMD_DONE_test__:" in script


class _FakeTerminalClient:
    def __init__(self, *, complete_commands: bool = True) -> None:
        self.terminals: dict[str, dict[str, Any]] = {}
        self.complete_commands = complete_commands
        self.next_id = 1
        self.writes: list[dict[str, str | None]] = []
        self.pending_output: dict[str, str] = {}

    async def create_terminal(
        self,
        *,
        session_id: str,
        title: str,
        agent_id: str | None = None,
        cwd: str | None = None,
        cols: int = 100,
        rows: int = 30,
    ) -> dict[str, Any]:
        terminal_id = f"terminal_{self.next_id}"
        self.next_id += 1
        terminal = {
            "terminal_id": terminal_id,
            "session_id": session_id,
            "title": title,
            "owner_agent_id": agent_id,
            "cwd": cwd,
            "cols": cols,
            "rows": rows,
            "status": "running",
            "last_command_status": None,
            "buffer": "",
        }
        self.terminals[terminal_id] = terminal
        self.pending_output[terminal_id] = ""
        return dict(terminal)

    async def get_terminal(self, terminal_id: str) -> dict[str, Any]:
        return dict(self.terminals[terminal_id])

    async def list_terminals(
        self,
        *,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            dict(terminal)
            for terminal in self.terminals.values()
            if session_id is None or terminal["session_id"] == session_id
        ]

    async def read_terminal(self, terminal_id: str) -> dict[str, Any]:
        output = self.pending_output[terminal_id]
        self.pending_output[terminal_id] = ""
        return {
            "terminal": dict(self.terminals[terminal_id]),
            "output": output,
            "sequence": 1,
            "replay_mode": "incremental",
            "omitted_before_sequence": None,
        }

    async def mark_terminal_backgrounded(self, terminal_id: str) -> dict[str, Any]:
        terminal = self.terminals[terminal_id]
        terminal["model_backgrounded"] = True
        return dict(terminal)

    async def write_terminal(
        self,
        *,
        terminal_id: str,
        data: str,
        source: str = "agent",
        command: str | None = None,
    ) -> dict[str, Any]:
        self.writes.append(
            {
                "terminal_id": terminal_id,
                "data": data,
                "source": source,
                "command": command,
            }
        )
        terminal = self.terminals[terminal_id]
        if command is None:
            terminal["buffer"] = f"{terminal['buffer']}{data}"
            self.pending_output[terminal_id] += data
            return dict(terminal)
        start_marker = re.search(r"(__BOXTEAM_CMD_START_[a-f0-9]+__)", data)
        done_marker = re.search(r"(__BOXTEAM_CMD_DONE_[a-f0-9]+__)", data)
        assert start_marker is not None
        assert done_marker is not None
        terminal["last_command"] = command
        terminal["last_command_status"] = "running"
        terminal["last_command_start_marker"] = start_marker.group(1)
        terminal["last_command_done_marker"] = done_marker.group(1)
        terminal["buffer"] = (
            f"{terminal['buffer']}\n{start_marker.group(1)}\n"
            "alpha beta gamma delta epsilon\n"
        )
        if self.complete_commands:
            terminal["buffer"] += f"{done_marker.group(1)}:0\n"
            terminal["last_command_status"] = "completed"
            terminal["last_command_exit_code"] = 0
        self.pending_output[terminal_id] += str(terminal["buffer"])
        return dict(terminal)

    async def kill_terminal(
        self,
        terminal_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        terminal = self.terminals[terminal_id]
        terminal["status"] = "terminated"
        terminal["release_reason"] = reason or "terminal_cancel"
        return {"killed": True, "terminal": dict(terminal)}

    async def delete_terminal(self, terminal_id: str) -> dict[str, Any]:
        terminal = self.terminals[terminal_id]
        terminal["status"] = "deleted"
        return {"deleted": True, "terminal": dict(terminal)}


def test_exec_command_schema_matches_codex() -> None:
    tool = create_exec_command_tool(
        session_id="session_1",
        terminal_client=_FakeTerminalClient(),  # type: ignore[arg-type]
    )

    schema = tool.args_schema.model_json_schema()

    assert set(schema["properties"]) == {
        "cmd",
        "workdir",
        "tty",
        "yield_time_ms",
        "max_output_tokens",
        "shell",
        "login",
    }
    assert schema["required"] == ["cmd"]
    assert schema["properties"]["yield_time_ms"]["default"] == 10_000


@pytest.mark.asyncio
async def test_exec_command_creates_independent_terminals_and_applies_options() -> None:
    client = _FakeTerminalClient()
    tool = create_exec_command_tool(
        session_id="session_1",
        terminal_client=client,  # type: ignore[arg-type]
    )

    first = await tool.ainvoke(
        {
            "cmd": "pwd",
            "workdir": "/tmp/project",
            "shell": "/bin/sh",
            "login": False,
            "yield_time_ms": 1,
            "max_output_tokens": 2,
        }
    )
    second = await tool.ainvoke({"cmd": "printf second", "yield_time_ms": 1})

    assert first["exit_code"] == 0
    assert "session_id" not in first
    assert first["chunk_id"] != second["chunk_id"]
    assert first["chunk_id"] != first["terminal_id"]
    assert first["original_token_count"] > 2
    assert len(first["output"]) < len("alpha beta gamma delta epsilon")
    assert client.terminals[first["terminal_id"]]["cwd"] == "/tmp/project"
    assert client.terminals[first["terminal_id"]]["status"] == "deleted"
    assert "cd -- /tmp/project && /bin/sh -c pwd" in str(client.writes[0]["data"])


@pytest.mark.asyncio
async def test_write_stdin_uses_model_session_id_as_terminal_id() -> None:
    client = _FakeTerminalClient()
    existing = await client.create_terminal(
        session_id="session_1",
        title="user terminal",
    )
    existing["last_command"] = "read value"
    existing["last_command_status"] = "running"
    client.terminals[str(existing["terminal_id"])] = existing
    tool = create_write_stdin_tool(
        session_id="session_1",
        terminal_client=client,  # type: ignore[arg-type]
    )

    result = await tool.ainvoke(
        {
            "session_id": existing["terminal_id"],
            "chars": "answer\r",
            "yield_time_ms": 1,
        }
    )

    assert result["session_id"] == existing["terminal_id"]
    assert result["terminal_id"] == existing["terminal_id"]
    assert result["chunk_id"] != existing["terminal_id"]
    assert client.writes[-1]["data"] == "answer\r"
    assert client.next_id == 2


@pytest.mark.asyncio
async def test_write_stdin_returns_output_generated_between_tool_calls() -> None:
    client = _FakeTerminalClient(complete_commands=False)
    exec_tool = create_exec_command_tool(
        session_id="session_1",
        terminal_client=client,  # type: ignore[arg-type]
    )
    first = await exec_tool.ainvoke({"cmd": "long task", "yield_time_ms": 1})
    terminal_id = first["session_id"]
    terminal = client.terminals[terminal_id]
    done_marker = terminal["last_command_done_marker"]
    client.pending_output[terminal_id] += f"between calls\n{done_marker}:0\n"
    terminal["last_command_status"] = "completed"
    terminal["last_command_exit_code"] = 0
    write_tool = create_write_stdin_tool(
        session_id="session_1",
        terminal_client=client,  # type: ignore[arg-type]
    )

    completed = await write_tool.ainvoke({"session_id": terminal_id})

    assert completed["output"] == "between calls"
    assert completed["exit_code"] == 0
    assert "session_id" not in completed
    assert client.terminals[terminal_id]["status"] == "deleted"


def test_write_stdin_schema_uses_trained_session_id_parameter() -> None:
    tool = create_write_stdin_tool(
        session_id="session_1",
        terminal_client=_FakeTerminalClient(),  # type: ignore[arg-type]
    )

    schema = tool.args_schema.model_json_schema()

    assert set(schema["properties"]) == {
        "session_id",
        "chars",
        "yield_time_ms",
        "max_output_tokens",
    }
    assert schema["required"] == ["session_id"]


@pytest.mark.asyncio
async def test_terminal_extension_tools_are_scoped_to_owner_session() -> None:
    client = _FakeTerminalClient()
    owned = await client.create_terminal(session_id="session_1", title="owned")
    await client.create_terminal(session_id="session_2", title="foreign")
    list_tool = create_list_terminal_sessions_tool(
        session_id="session_1",
        terminal_client=client,  # type: ignore[arg-type]
    )
    kill_tool = create_kill_terminal_tool(
        session_id="session_1",
        terminal_client=client,  # type: ignore[arg-type]
    )

    listed = await list_tool.ainvoke({})
    killed = await kill_tool.ainvoke({"session_id": owned["terminal_id"]})

    assert [item["session_id"] for item in listed["sessions"]] == [
        owned["terminal_id"]
    ]
    assert killed["status"] == "terminated"
    assert killed["release_reason"] == "model_requested"
