from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.agents.tools.terminal import (
    _resolve_terminal_workdir,
    create_exec_command_tool,
    create_kill_terminal_tool,
    create_list_terminal_sessions_tool,
    create_write_stdin_tool,
)
from app.agents.tools.terminal_command import _build_terminal_input
from app.agents.tools.terminal_contract import tool_output
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.core.turn_execution_scope import (
    TurnExecutionScope,
    reset_current_turn_execution_scope,
    set_current_turn_execution_scope,
)
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.business.terminal_resources.owner import TerminalResourceOwner
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.support.resource_owners.sessions import (
    SESSION_A,
    SESSION_B,
    THREAD_A,
    THREAD_B,
    WORKSPACE_ID,
    ResourceOwnerSessionsFake,
)

SOURCE_THREAD_ID = THREAD_A


def _thread_binding(session_id: str = SESSION_A) -> ThreadRuntimeBinding:
    return ThreadRuntimeBinding(session_id=session_id, thread_id=SOURCE_THREAD_ID)


def test_windows_cmd_wrapper_uses_cmd_syntax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")

    terminal_input = _build_terminal_input(
        command_text="echo BOXTEAM_CMD_OK",
        working_directory=r"C:\workspace\demo",
        shell_executable=None,
        login=True,
        start_marker="__BOXTEAM_CMD_START_test__",
        done_marker="__BOXTEAM_CMD_DONE_test__",
    )

    assert "${SHELL" not in terminal_input
    assert "printf" not in terminal_input
    assert 'cd /d "C:\\workspace\\demo"' in terminal_input
    assert "echo __BOXTEAM_CMD_START_test__" in terminal_input
    assert 'set "__BOXTEAM_RC=%ERRORLEVEL%"' in terminal_input
    assert "echo __BOXTEAM_CMD_DONE_test__:%__BOXTEAM_RC%" in terminal_input


def test_windows_powershell_wrapper_preserves_command_and_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    terminal_input = _build_terminal_input(
        command_text='Write-Output "中文"',
        working_directory=r"C:\workspace\demo",
        shell_executable=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        login=True,
        start_marker="__BOXTEAM_CMD_START_test__",
        done_marker="__BOXTEAM_CMD_DONE_test__",
    )

    encoded_script = terminal_input.rstrip("\r").split()[-1]
    script = base64.b64decode(encoded_script).decode("utf-16le")
    assert "${SHELL" not in terminal_input
    assert "-EncodedCommand" in terminal_input
    assert "Set-Location -LiteralPath 'C:\\workspace\\demo'" in script
    assert "Write-Output '__BOXTEAM_CMD_START_test__'" in script
    encoded_command = base64.b64encode('Write-Output "中文"'.encode()).decode()
    assert encoded_command in script
    assert "__BOXTEAM_CMD_DONE_test__:" in script


def test_terminal_output_marks_port_conflict_as_retryable_environment_issue() -> None:
    result = tool_output(
        terminal_id="terminal_port_conflict",
        wall_time_seconds=0.1,
        output="OSError: [Errno 98] Address already in use",
        max_output_tokens=None,
        exit_code=1,
        running=False,
        environment_issue={
            "code": "port_in_use",
            "retryable": True,
            "message": "命令尝试绑定已占用端口；请先复用现有服务，或选择明确空闲端口。",
            "recovery": "reuse_existing_service_or_choose_free_port",
        },
    )

    assert result["exit_code"] == 1
    assert result["status"] == "error"
    assert result["environment_issue"]["code"] == "port_in_use"
    assert result["environment_issue"]["retryable"] is True


class _FakeTerminalClient:
    def __init__(self, root: Path, *, complete_commands: bool = True) -> None:
        self.root = root
        self.created_cwds: dict[str, str] = {}
        self.terminals: dict[str, dict[str, Any]] = {}
        self.complete_commands = complete_commands
        self.next_id = 1
        self.writes: list[dict[str, str | None]] = []
        self.pending_output: dict[str, str] = {}

    async def create_terminal(
        self,
        *,
        terminal_id: str,
        owner_revision: int,
        title: str,
        agent_id: str | None = None,
        cwd: str | None = None,
        cols: int = 100,
        rows: int = 30,
    ) -> dict[str, Any]:
        self.next_id += 1
        terminal = {
            "terminal_id": terminal_id,
            "workspace_id": WORKSPACE_ID,
            "transport_owner_revision": owner_revision,
            "reconciliation_status": "verified",
            "created_at": "2026-10-05T00:00:00Z",
            "updated_at": "2026-10-05T00:00:00Z",
            "title": title,
            "owner_agent_id": agent_id,
            "cwd_relative": os.path.relpath(cwd or str(self.root), self.root),
            "cols": cols,
            "rows": rows,
            "status": "running",
            "last_command_status": None,
            "buffer": "",
        }
        self.terminals[terminal_id] = terminal
        self.created_cwds[terminal_id] = cwd or str(self.root)
        self.pending_output[terminal_id] = ""
        return dict(terminal)

    async def get_resource(self, terminal_id: str) -> dict[str, Any]:
        return dict(self.terminals[terminal_id])

    async def list_resources(self) -> list[dict[str, Any]]:
        return [dict(terminal) for terminal in self.terminals.values()]

    async def sync_owner_revision(self, terminal_id: str, owner_revision: int) -> None:
        self.terminals[terminal_id]["transport_owner_revision"] = owner_revision

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

    async def stop_resource(
        self,
        terminal_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        terminal = self.terminals[terminal_id]
        terminal["status"] = "terminated"
        terminal["release_reason"] = reason or "terminal_cancel"
        return {"killed": True, "terminal": dict(terminal)}

    async def delete_resource(self, terminal_id: str) -> dict[str, Any]:
        terminal = self.terminals[terminal_id]
        terminal["status"] = "deleted"
        return {"deleted": True, "terminal": dict(terminal)}


@dataclass
class TerminalEnvironment:
    root: Path
    client: _FakeTerminalClient
    owner: TerminalResourceOwner
    context: ToolInvocationContext


@pytest.fixture
def terminal_env(tmp_path: Path) -> Iterator[TerminalEnvironment]:
    sessions = ResourceOwnerSessionsFake(tmp_path)
    client = _FakeTerminalClient(tmp_path)
    owner = TerminalResourceOwner(
        workspace_root=tmp_path, workspace_id=WORKSPACE_ID, sessions=sessions,
        threads=sessions, transport=client, leases=ExternalResourceLeaseLedger(),
        thread_release_fence=sessions.accepts_release,
    )
    context = ToolInvocationContext(thread_binding=_thread_binding())
    admission = ExecutionAdmission(
        session_id=SESSION_A, thread_id=THREAD_A, execution_id="execution-terminal-test",
        job_id="job-terminal-test", admission_idempotency_key="admission-terminal-test",
        execution_origin="internal", turn_id=None,
    )
    tool_token = context.set_tool_call_id("tool-call-terminal-test")
    scope_token = set_current_turn_execution_scope(TurnExecutionScope(admission.execution_id))
    with (
        session_thread_owner_scope(MutationIntentOwner(SESSION_A, THREAD_A), langgraph_thread_id=THREAD_A),
        execution_admission_scope(admission, langgraph_thread_id=THREAD_A),
    ):
        try:
            yield TerminalEnvironment(tmp_path, client, owner, context)
        finally:
            context.reset_tool_call_id(tool_token)
            reset_current_turn_execution_scope(scope_token)


def test_exec_command_schema_matches_codex(terminal_env: TerminalEnvironment) -> None:
    tool = create_exec_command_tool(
        session_id=SESSION_A,
        terminal_owner=terminal_env.owner, invocation_context=terminal_env.context,
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
        "retention_scope",
    }
    assert schema["required"] == ["cmd"]
    assert schema["properties"]["yield_time_ms"]["default"] == 10_000
    assert schema["properties"]["retention_scope"]["default"] == "thread"
    assert schema["properties"]["retention_scope"]["enum"] == ["thread", "workspace"]


@pytest.mark.asyncio
async def test_exec_command_creates_independent_terminals_and_applies_options(terminal_env: TerminalEnvironment) -> None:
    client = terminal_env.client
    workspace_root = terminal_env.root
    tool = create_exec_command_tool(
        session_id=SESSION_A,
        terminal_owner=terminal_env.owner, invocation_context=terminal_env.context,
        workspace_root=workspace_root,
    )

    first = await tool.ainvoke(
        {
            "cmd": "pwd",
            "workdir": "parry_arena",
            "shell": "/bin/sh",
            "login": False,
            "yield_time_ms": 1,
            "max_output_tokens": 2,
        }
    )
    second = await tool.ainvoke({
        "cmd": "printf second",
        "yield_time_ms": 1,
        "retention_scope": "workspace",
    })

    assert first["exit_code"] == 0
    assert "session_id" not in first
    assert first["chunk_id"] != second["chunk_id"]
    assert first["chunk_id"] != first["terminal_id"]
    assert first["original_token_count"] > 2
    assert len(first["output"]) < len("alpha beta gamma delta epsilon")
    assert client.created_cwds[first["terminal_id"]] == str(
        workspace_root / "parry_arena"
    )
    # 模型可见载荷只承载工作区内相对目录，MUST NOT 出现绝对真实路径（6.3）。
    assert first["cwd_relative"] == "parry_arena"
    assert workspace_root.as_posix() not in json.dumps(first, default=str)
    assert client.terminals[first["terminal_id"]]["status"] == "deleted"
    assert (await terminal_env.owner.get_resource(first["terminal_id"]))["source_thread_id"] == SOURCE_THREAD_ID
    assert (await terminal_env.owner.get_resource(first["terminal_id"]))["retention_scope"] == "thread"
    assert "/bin/sh -c pwd" in str(client.writes[0]["data"])
    assert "cd --" not in str(client.writes[0]["data"])
    assert second["cwd_relative"] == "."
    assert client.created_cwds[second["terminal_id"]] == str(workspace_root)
    assert (await terminal_env.owner.get_resource(second["terminal_id"]))["retention_scope"] == "workspace"


@pytest.mark.asyncio
async def test_exec_command_requires_trusted_thread_binding_before_create(terminal_env: TerminalEnvironment) -> None:
    client = terminal_env.client
    tool = create_exec_command_tool(
        session_id=SESSION_A,
        terminal_owner=terminal_env.owner, invocation_context=terminal_env.context,
    )

    terminal_env.context.thread_binding = None
    with pytest.raises(RuntimeError, match="ThreadRuntimeBinding"):
        await tool.ainvoke({"cmd": "pwd"})

    assert client.next_id == 1


def test_resolves_relative_workdir_once_against_workspace_root(tmp_path: Path) -> None:
    assert _resolve_terminal_workdir(
        "parry_arena",
        workspace_root=tmp_path,
    ) == str((tmp_path / "parry_arena").resolve())


def test_resolve_terminal_workdir_honors_absolute_path_outside_workspace(
    tmp_path: Path,
) -> None:
    """contract：相对 workdir 锚定 workspace 根；显式绝对路径按原样采用。

    PTY 不提供文件沙箱（cmd 本身即可访问任意路径），因此 workdir 不做
    workspace 边界校验；此处锁定该现状，避免后续把它误当作安全边界。
    """
    assert _resolve_terminal_workdir("/etc", workspace_root=tmp_path) == "/etc"
    assert _resolve_terminal_workdir(
        "..",
        workspace_root=tmp_path / "nested" / "proj",
    ) == str((tmp_path / "nested").resolve())


@pytest.mark.asyncio
async def test_write_stdin_uses_model_session_id_as_terminal_id(terminal_env: TerminalEnvironment) -> None:
    client = terminal_env.client
    existing = await terminal_env.owner.create_terminal(
        session_id=SESSION_A,
        thread_id=SOURCE_THREAD_ID,
        retention_scope="thread",
        title="user terminal",
        holder_id="request:terminal-setup", operation_id="terminal-setup",
    )
    existing["last_command"] = "read value"
    existing["last_command_status"] = "running"
    client.terminals[str(existing["terminal_id"])].update({"last_command": "read value", "last_command_status": "running"})
    tool = create_write_stdin_tool(
        session_id=SESSION_A,
        terminal_owner=terminal_env.owner, invocation_context=terminal_env.context,
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
async def test_write_stdin_returns_output_generated_between_tool_calls(terminal_env: TerminalEnvironment) -> None:
    client = terminal_env.client
    client.complete_commands = False
    exec_tool = create_exec_command_tool(
        session_id=SESSION_A,
        terminal_owner=terminal_env.owner, invocation_context=terminal_env.context,
    )
    first = await exec_tool.ainvoke({"cmd": "long task", "yield_time_ms": 1})
    terminal_id = first["session_id"]
    terminal = client.terminals[terminal_id]
    done_marker = terminal["last_command_done_marker"]
    client.pending_output[terminal_id] += f"between calls\n{done_marker}:0\n"
    terminal["last_command_status"] = "completed"
    terminal["last_command_exit_code"] = 0
    write_tool = create_write_stdin_tool(
        session_id=SESSION_A,
        terminal_owner=terminal_env.owner, invocation_context=terminal_env.context,
    )

    completed = await write_tool.ainvoke({"session_id": terminal_id})

    assert completed["output"] == "between calls"
    assert completed["exit_code"] == 0
    assert "session_id" not in completed
    assert client.terminals[terminal_id]["status"] == "deleted"


def test_write_stdin_schema_uses_trained_session_id_parameter(terminal_env: TerminalEnvironment) -> None:
    tool = create_write_stdin_tool(
        session_id=SESSION_A,
        terminal_owner=terminal_env.owner, invocation_context=terminal_env.context,
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
async def test_terminal_extension_tools_are_scoped_to_owner_session(terminal_env: TerminalEnvironment) -> None:
    owned = await terminal_env.owner.create_terminal(
        session_id=SESSION_A,
        thread_id=SOURCE_THREAD_ID,
        retention_scope="thread",
        title="owned",
        holder_id="request:terminal-setup", operation_id="terminal-setup",
    )
    await terminal_env.owner.create_terminal(
        session_id=SESSION_B,
        thread_id=THREAD_B,
        retention_scope="thread",
        title="foreign",
        holder_id="request:terminal-setup", operation_id="terminal-setup",
    )
    list_tool = create_list_terminal_sessions_tool(
        session_id=SESSION_A,
        terminal_owner=terminal_env.owner, invocation_context=terminal_env.context,
    )
    kill_tool = create_kill_terminal_tool(
        session_id=SESSION_A,
        terminal_owner=terminal_env.owner, invocation_context=terminal_env.context,
    )

    listed = await list_tool.ainvoke({})
    killed = await kill_tool.ainvoke({"session_id": owned["terminal_id"]})

    assert [item["session_id"] for item in listed["sessions"]] == [
        owned["terminal_id"]
    ]
    assert killed["status"] == "terminated"
    assert killed["release_reason"] == "model_requested"
