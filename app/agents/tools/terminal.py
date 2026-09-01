from __future__ import annotations

import asyncio
import base64
import os
import shlex
from pathlib import Path
from time import monotonic
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.tools.terminal_contract import (
    DEFAULT_EXEC_YIELD_TIME_MS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_WRITE_STDIN_YIELD_TIME_MS,
    classify_terminal_environment_issue,
    clean_terminal_delta,
    effective_yield_time_ms,
    extract_command_output,
    tool_output,
    truncate_output,
    validate_max_output_tokens,
)
from app.core.identifier import create_uuid_hex
from app.core.path_utils import get_workspace_root
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient


def _command_for_posix_shell(
    *,
    cmd: str,
    workdir: str | None,
    shell: str | None,
    login: bool,
) -> str:
    shell_command = shlex.quote(shell) if shell else '"${SHELL:-/bin/bash}"'
    shell_flag = "-lc" if login else "-c"
    invocation = f"{shell_command} {shell_flag} {shlex.quote(cmd)}"
    if workdir is None:
        return invocation
    return f"cd -- {shlex.quote(workdir)} && {invocation}"


def _windows_shell_name(shell: str | None) -> str:
    configured = shell or os.environ.get("COMSPEC") or "cmd.exe"
    return configured.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()


def _windows_cmd_quote(value: str) -> str:
    if '"' in value:
        raise ValueError("Windows 命令路径不能包含双引号")
    return f'"{value}"'


def _windows_powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_powershell_command(
    *,
    cmd: str,
    workdir: str | None,
    shell: str,
    start_marker: str,
    done_marker: str,
) -> str:
    command_payload = base64.b64encode(cmd.encode("utf-8")).decode("ascii")
    script_lines = [
        f"Write-Output {_windows_powershell_quote(start_marker)}",
        "$boxteamExitCode = 0",
        "try {",
    ]
    if workdir is not None:
        script_lines.append(
            "  Set-Location -LiteralPath "
            f"{_windows_powershell_quote(workdir)}"
        )
    script_lines.extend(
        [
            "  $global:LASTEXITCODE = 0",
            (
                "  $boxteamCommand = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("
                f"{_windows_powershell_quote(command_payload)}))"
            ),
            "  Invoke-Expression -Command $boxteamCommand",
            "  $boxteamSucceeded = $?",
            "  $boxteamExitCode = [int]$LASTEXITCODE",
            "  if (-not $boxteamSucceeded -and $boxteamExitCode -eq 0) {",
            "    $boxteamExitCode = 1",
            "  }",
            "} catch {",
            "  Write-Error $_",
            "  $boxteamExitCode = 1",
            "}",
            (
                "Write-Output ("
                f"{_windows_powershell_quote(done_marker + ':')}"
                " + $boxteamExitCode)"
            ),
        ]
    )
    encoded_script = base64.b64encode(
        "\n".join(script_lines).encode("utf-16le")
    ).decode("ascii")
    return (
        f"{_windows_cmd_quote(shell)} -NoLogo -NoProfile -NonInteractive "
        f"-ExecutionPolicy Bypass -EncodedCommand {encoded_script}\r"
    )


def _command_for_windows_shell(
    *,
    cmd: str,
    workdir: str | None,
    shell: str | None,
    start_marker: str,
    done_marker: str,
) -> str:
    shell_name = _windows_shell_name(shell)
    shell_path = shell or os.environ.get("COMSPEC") or "cmd.exe"
    if shell_name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
        return _windows_powershell_command(
            cmd=cmd,
            workdir=workdir,
            shell=shell_path,
            start_marker=start_marker,
            done_marker=done_marker,
        )
    if shell_name not in {"cmd", "cmd.exe"}:
        raise ValueError(
            "Windows 终端暂不支持该 shell；请使用 cmd.exe、powershell.exe 或 pwsh.exe: "
            f"{shell_path}"
        )

    lines = [f"echo {start_marker}"]
    if workdir is not None:
        lines.append(f"cd /d {_windows_cmd_quote(workdir)}")
    lines.extend(
        [
            cmd,
            'set "__BOXTEAM_RC=%ERRORLEVEL%"',
            f"echo {done_marker}:%__BOXTEAM_RC%",
        ]
    )
    return "\r\n".join(lines) + "\r"


def _command_for_shell(
    *,
    cmd: str,
    workdir: str | None,
    shell: str | None,
    login: bool,
    start_marker: str,
    done_marker: str,
) -> str:
    if os.name == "nt":
        return _command_for_windows_shell(
            cmd=cmd,
            workdir=workdir,
            shell=shell,
            start_marker=start_marker,
            done_marker=done_marker,
        )
    return _command_for_posix_shell(
        cmd=cmd,
        workdir=workdir,
        shell=shell,
        login=login,
    )


def _resolve_terminal_workdir(
    workdir: str | None,
    *,
    workspace_root: Path | None,
) -> str:
    """将模型的工作区相对路径解析为 PTY 唯一使用的绝对 cwd。"""
    root = (workspace_root or get_workspace_root()).resolve()
    raw_workdir = workdir.strip() if isinstance(workdir, str) else ""
    if not raw_workdir:
        return str(root)
    requested = Path(raw_workdir).expanduser()
    if not requested.is_absolute():
        requested = root / requested
    return str(requested.resolve())


async def _get_owned_terminal(
    *,
    terminal_client: TerminalManagerClient,
    terminal_id: str,
    owner_session_id: str,
) -> dict[str, Any]:
    terminal = await terminal_client.get_terminal(terminal_id)
    if terminal.get("session_id") != owner_session_id:
        raise ValueError(
            "session_id 不属于当前 Agent 会话: "
            f"session_id={terminal_id}, owner_session_id={owner_session_id}"
        )
    return terminal


async def _read_pending_output(
    *,
    terminal_client: TerminalManagerClient,
    terminal_id: str,
) -> tuple[str, dict[str, Any]]:
    read = await terminal_client.read_terminal(terminal_id)
    output = read.get("output")
    terminal = read.get("terminal")
    if not isinstance(output, str) or not isinstance(terminal, dict):
        raise TypeError(f"终端输出读取结果无效: terminal_id={terminal_id}")
    omitted_before_sequence = read.get("omitted_before_sequence")
    if omitted_before_sequence is not None:
        output = (
            "[... 早期终端输出已超出重放窗口，以下为当前缓冲区快照 ...]\n"
            f"{output}"
        )
    return output, terminal


def create_exec_command_tool(
    session_id: str,
    agent_id: str = "default",
    *,
    terminal_client: TerminalManagerClient,
    workspace_root: Path | None = None,
) -> BaseTool:
    """创建 Codex 风格的持久终端命令工具。"""

    @tool("exec_command")
    async def exec_command(
        cmd: str,
        workdir: str | None = None,
        tty: bool = False,
        yield_time_ms: int = DEFAULT_EXEC_YIELD_TIME_MS,
        max_output_tokens: int | None = None,
        shell: str | None = None,
        login: bool = True,
    ) -> dict[str, Any]:
        """运行命令；未在等待窗口内结束时返回可供 write_stdin 使用的 session_id。

        workdir 是相对于 workspace 根目录的一次性 PTY cwd，不要在 cmd 中再次 cd 到该目录。
        Godot 使用 ``--path project`` 时，``--export-release`` 的输出路径相对于 project；
        例如从 workspace 根运行时使用 ``godot_export/game.html``，不要重复写成
        ``project/godot_export/game.html``。
        """
        if not cmd.strip():
            raise ValueError("cmd 不能为空")
        if shell is not None and not shell.strip():
            raise ValueError("shell 不能为空")
        if "\x00" in (shell or ""):
            raise ValueError("shell 不能包含空字符")
        validate_max_output_tokens(max_output_tokens)
        resolved_yield_time_ms = effective_yield_time_ms(yield_time_ms)
        started_at = monotonic()
        resolved_workdir = _resolve_terminal_workdir(
            workdir,
            workspace_root=workspace_root,
        )

        terminal = await terminal_client.create_terminal(
            session_id=session_id,
            title=f"{agent_id} terminal",
            agent_id=agent_id,
            cwd=resolved_workdir,
        )
        terminal_id = str(terminal["terminal_id"])
        run_id = create_uuid_hex()
        start_marker = f"__BOXTEAM_CMD_START_{run_id}__"
        done_marker = f"__BOXTEAM_CMD_DONE_{run_id}__"
        shell_command = _command_for_shell(
            cmd=cmd,
            # cwd 已由 PTY 创建请求设置；这里不能再次 cd，否则相对 workdir
            # 会从已进入的目录重复拼接（例如 workspace/parry_arena/parry_arena）。
            workdir=None,
            shell=shell,
            login=login,
            start_marker=start_marker,
            done_marker=done_marker,
        )
        # TODO: 兼容 Codex 参数；BoxTeam 为保证 session_id 可 attach，底层始终分配 PTY。
        _ = tty
        wrapped_command = shell_command
        if os.name != "nt":
            wrapped_command = (
                f"printf '\\n{start_marker}\\n'; "
                f"{shell_command}; "
                f"__boxteam_rc=$?; "
                f"printf '\\n{done_marker}:%s\\n' \"$__boxteam_rc\"\r"
            )
        try:
            await terminal_client.write_terminal(
                terminal_id=terminal_id,
                data=wrapped_command,
                source="agent",
                command=cmd,
            )
        except BaseException as write_error:
            try:
                await terminal_client.delete_terminal(terminal_id)
            except BaseException as cleanup_error:
                raise RuntimeError(
                    "终端命令写入失败且清理 execution 也失败: "
                    f"terminal_id={terminal_id}, write_error={write_error}, "
                    f"cleanup_error={cleanup_error}"
                ) from cleanup_error
            raise

        deadline = asyncio.get_running_loop().time() + resolved_yield_time_ms / 1000
        consumed_output = ""
        while True:
            pending_output, _latest_terminal = await _read_pending_output(
                terminal_client=terminal_client,
                terminal_id=terminal_id,
            )
            consumed_output += pending_output
            completed, raw_output, exit_code = extract_command_output(
                buffer=consumed_output,
                start_marker=start_marker,
                done_marker=done_marker,
            )
            if completed:
                await terminal_client.delete_terminal(terminal_id)
                result = tool_output(
                    terminal_id=terminal_id,
                    wall_time_seconds=monotonic() - started_at,
                    output=raw_output,
                    max_output_tokens=max_output_tokens,
                    exit_code=exit_code,
                    running=False,
                    environment_issue=classify_terminal_environment_issue(
                        raw_output,
                        exit_code,
                    ),
                )
                result["cwd"] = resolved_workdir
                return result
            remaining_seconds = deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                break
            await asyncio.sleep(min(0.25, remaining_seconds))

        _, raw_output, _ = extract_command_output(
            buffer=consumed_output,
            start_marker=start_marker,
            done_marker=done_marker,
        )
        try:
            await terminal_client.mark_terminal_backgrounded(terminal_id)
        except BaseException as background_error:
            try:
                await terminal_client.delete_terminal(terminal_id)
            except BaseException as cleanup_error:
                raise RuntimeError(
                    "终端后台注册失败且清理 execution 也失败: "
                    f"terminal_id={terminal_id}, background_error={background_error}, "
                    f"cleanup_error={cleanup_error}"
                ) from cleanup_error
            raise
        result = tool_output(
            terminal_id=terminal_id,
            wall_time_seconds=monotonic() - started_at,
            output=raw_output,
            max_output_tokens=max_output_tokens,
            running=True,
        )
        result["cwd"] = resolved_workdir
        return result

    return exec_command


def create_write_stdin_tool(
    session_id: str,
    *,
    terminal_client: TerminalManagerClient,
) -> BaseTool:
    """创建 Codex 风格的持续终端交互工具。"""

    owner_session_id = session_id

    @tool("write_stdin")
    async def write_stdin(
        session_id: str,
        chars: str = "",
        yield_time_ms: int = DEFAULT_WRITE_STDIN_YIELD_TIME_MS,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        """向 exec_command 返回的 session_id 写入字符；空字符用于轮询后台命令。"""
        validate_max_output_tokens(max_output_tokens)
        resolved_yield_time_ms = effective_yield_time_ms(
            yield_time_ms,
            empty_poll=not chars,
        )
        started_at = monotonic()
        terminal = await _get_owned_terminal(
            terminal_client=terminal_client,
            terminal_id=session_id,
            owner_session_id=owner_session_id,
        )
        if (
            terminal.get("status") != "running"
            and terminal.get("last_command_status") != "completed"
        ):
            raise ValueError(
                f"终端未运行: session_id={session_id}, status={terminal.get('status')}"
                f", release_reason={terminal.get('release_reason')}"
            )
        command_status = terminal.get("last_command_status")
        if chars and command_status not in {None, "running"}:
            raise ValueError(
                f"命令已经结束，不能继续写入: session_id={session_id}, "
                f"command_status={command_status}"
            )

        consumed_output, latest_terminal = await _read_pending_output(
            terminal_client=terminal_client,
            terminal_id=session_id,
        )
        if chars:
            await terminal_client.write_terminal(
                terminal_id=session_id,
                data=chars,
                source="agent",
            )

        deadline = asyncio.get_running_loop().time() + resolved_yield_time_ms / 1000
        while latest_terminal.get("last_command_status") == "running":
            remaining_seconds = deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                break
            await asyncio.sleep(min(0.25, remaining_seconds))
            pending_output, latest_terminal = await _read_pending_output(
                terminal_client=terminal_client,
                terminal_id=session_id,
            )
            consumed_output += pending_output
            if latest_terminal.get("last_command_status") != "running":
                break

        output = clean_terminal_delta(consumed_output, latest_terminal)
        latest_command_status = latest_terminal.get("last_command_status")
        user_owned_shell = latest_terminal.get("last_command") is None
        running = latest_command_status == "running" or user_owned_shell
        raw_exit_code = latest_terminal.get("last_command_exit_code")
        exit_code = raw_exit_code if isinstance(raw_exit_code, int) else None
        result = tool_output(
            terminal_id=session_id,
            wall_time_seconds=monotonic() - started_at,
            output=output,
            max_output_tokens=max_output_tokens,
            exit_code=exit_code,
            running=running,
            environment_issue=classify_terminal_environment_issue(output, exit_code),
        )
        if not running:
            await terminal_client.delete_terminal(session_id)
        return result

    return write_stdin


def create_list_terminal_sessions_tool(
    session_id: str,
    *,
    terminal_client: TerminalManagerClient,
) -> BaseTool:
    """创建当前 Agent Session 的终端执行列表工具。"""

    @tool("list_terminal_sessions")
    async def list_terminal_sessions(
        include_completed: bool = False,
    ) -> dict[str, Any]:
        """列出当前 Agent Session 拥有的终端执行；默认只返回活动执行。"""
        terminals = await terminal_client.list_terminals(session_id=session_id)
        active_statuses = {"created", "running"}
        visible = [
            terminal
            for terminal in terminals
            if include_completed or terminal.get("status") in active_statuses
        ]
        if include_completed:
            visible = visible[:64]
        return {
            "sessions": [
                {
                    "session_id": terminal.get("terminal_id"),
                    "terminal_id": terminal.get("terminal_id"),
                    "status": terminal.get("status"),
                    "command_status": terminal.get("last_command_status"),
                    "command": terminal.get("last_command"),
                    "cwd": terminal.get("cwd"),
                    "created_at": terminal.get("created_at"),
                    "last_used_at": terminal.get("last_used_at"),
                    "release_reason": terminal.get("release_reason"),
                }
                for terminal in visible
            ]
        }

    return list_terminal_sessions


def create_kill_terminal_tool(
    session_id: str,
    *,
    terminal_client: TerminalManagerClient,
) -> BaseTool:
    """创建终止当前 Agent Session 所属终端执行的工具。"""

    owner_session_id = session_id

    @tool("kill_terminal")
    async def kill_terminal(session_id: str) -> dict[str, Any]:
        """终止 exec_command 返回的 session_id 及其完整进程树。"""
        terminal = await _get_owned_terminal(
            terminal_client=terminal_client,
            terminal_id=session_id,
            owner_session_id=owner_session_id,
        )
        pending_output, _ = await _read_pending_output(
            terminal_client=terminal_client,
            terminal_id=session_id,
        )
        if terminal.get("status") == "running":
            killed = await terminal_client.kill_terminal(
                session_id,
                reason="model_requested",
            )
            killed_terminal = killed.get("terminal")
            if isinstance(killed_terminal, dict):
                terminal = killed_terminal
        final_output, _ = await _read_pending_output(
            terminal_client=terminal_client,
            terminal_id=session_id,
        )
        output, original_token_count = truncate_output(
            pending_output + final_output,
            DEFAULT_MAX_OUTPUT_TOKENS,
        )
        return {
            "session_id": session_id,
            "terminal_id": session_id,
            "status": terminal.get("status"),
            "release_reason": terminal.get("release_reason") or "model_requested",
            "original_token_count": original_token_count,
            "output": output,
        }

    return kill_terminal
