from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool

from app.agents.tool_invocation_context import ToolInvocationContext
from app.agents.tools.terminal_command import _build_terminal_input
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
from app.core.execution_admission_context import require_current_execution_admission
from app.core.identifier import create_uuid_hex
from app.core.turn_execution_scope import get_current_turn_execution_scope
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.business.terminal_resources.owner import TerminalResourceOwner


@asynccontextmanager
async def _terminal_operation(
    owner: TerminalResourceOwner, session_id: str, context: ToolInvocationContext,
) -> AsyncIterator[TerminalOperations]:
    binding = context.thread_binding
    if binding is None or binding.session_id != session_id:
        raise RuntimeError("Terminal 工具缺少匹配会话的真实 ThreadRuntimeBinding")
    admission = require_current_execution_admission(langgraph_thread_id=binding.thread_id)
    if (admission.session_id, admission.thread_id) != (binding.session_id, binding.thread_id):
        raise RuntimeError("Terminal 工具 binding 与 execution admission 冲突")
    scope = get_current_turn_execution_scope()
    if scope is None or scope.is_closed:
        raise RuntimeError("Terminal 工具缺少活动 execution scope")
    scope.raise_if_cancelled()
    async with owner.operation(
        session_id=binding.session_id, thread_id=binding.thread_id,
        holder_id=scope.turn_stream_id, operation_id=context.require_tool_call_id(),
        created_by_turn_id=admission.turn_id,
    ) as operations:
        yield operations


async def _read_pending_output(
    *,
    terminal_client: TerminalOperations,
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
    terminal_owner: TerminalResourceOwner,
    invocation_context: ToolInvocationContext,
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
        retention_scope: Literal["thread", "workspace"] = "thread",
    ) -> dict[str, Any]:
        """运行命令；未在等待窗口内结束时返回可供 write_stdin 使用的 session_id。

        workdir 是一次性 PTY cwd：接受目标工作区目录 URI、相对路径和原生绝对路径；
        相对路径固定以 workspace 根目录解析。不要在 cmd 中再次 cd 到该目录。
        Godot 使用 ``--path project`` 时，``--export-release`` 的输出路径相对于 project；
        例如从 workspace 根运行时使用 ``godot_export/game.html``，不要重复写成
        ``project/godot_export/game.html``。
        """
        async with _terminal_operation(terminal_owner, session_id, invocation_context) as terminal_client:
            if not cmd.strip():
                raise ValueError("cmd 不能为空")
            if shell is not None and not shell.strip():
                raise ValueError("shell 不能为空")
            if "\x00" in (shell or ""):
                raise ValueError("shell 不能包含空字符")
            validate_max_output_tokens(max_output_tokens)
            resolved_yield_time_ms = effective_yield_time_ms(yield_time_ms)
            started_at = monotonic()
            terminal = await terminal_client.create_terminal(
                retention_scope=retention_scope,
                title=f"{agent_id} terminal",
                agent_id=agent_id,
                cwd=workdir.strip() if workdir is not None else None,
            )
            terminal_id = str(terminal["terminal_id"])
            run_id = create_uuid_hex()
            start_marker = f"__BOXTEAM_CMD_START_{run_id}__"
            done_marker = f"__BOXTEAM_CMD_DONE_{run_id}__"
            terminal_input = _build_terminal_input(
                command_text=cmd,
                # cwd 已由 PTY 创建请求设置；这里不能再次 cd，否则相对 workdir
                # 会从已进入的目录重复拼接（例如 workspace/parry_arena/parry_arena）。
                working_directory=None,
                shell_executable=shell,
                login=login,
                start_marker=start_marker,
                done_marker=done_marker,
            )
            # TODO: 兼容 Codex 参数；BoxTeam 为保证 session_id 可 attach，底层始终分配 PTY。
            _ = tty
            command_input = terminal_input
            if os.name != "nt":
                command_input = (
                    f"printf '\\n{start_marker}\\n'; "
                    f"{terminal_input}; "
                    f"__boxteam_rc=$?; "
                    f"printf '\\n{done_marker}:%s\\n' \"$__boxteam_rc\"\r"
                )
            await terminal_client.write_terminal(
                terminal_id=terminal_id,
                data=command_input,
                source="agent",
                command=cmd,
            )
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
                    await terminal_client.complete_terminal(terminal_id)
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
                    result["cwd_relative"] = terminal["cwd_relative"]
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
            await terminal_client.mark_terminal_backgrounded(terminal_id)
            result = tool_output(
                terminal_id=terminal_id,
                wall_time_seconds=monotonic() - started_at,
                output=raw_output,
                max_output_tokens=max_output_tokens,
                running=True,
            )
            result["cwd_relative"] = terminal["cwd_relative"]
            return result

    return exec_command


def create_write_stdin_tool(
    session_id: str,
    *,
    terminal_owner: TerminalResourceOwner,
    invocation_context: ToolInvocationContext,
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
        async with _terminal_operation(terminal_owner, owner_session_id, invocation_context) as terminal_client:
            validate_max_output_tokens(max_output_tokens)
            resolved_yield_time_ms = effective_yield_time_ms(
                yield_time_ms,
                empty_poll=not chars,
            )
            started_at = monotonic()
            terminal = await terminal_client.get_resource(session_id)
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
                await terminal_client.complete_terminal(session_id)
            return result

    return write_stdin


def create_list_terminal_sessions_tool(
    session_id: str,
    *,
    terminal_owner: TerminalResourceOwner,
    invocation_context: ToolInvocationContext,
) -> BaseTool:
    """创建当前 Agent Session 的终端执行列表工具。"""

    @tool("list_terminal_sessions")
    async def list_terminal_sessions(
        include_completed: bool = False,
    ) -> dict[str, Any]:
        """列出当前 Agent Session 拥有的终端执行；默认只返回活动执行。"""
        async with _terminal_operation(terminal_owner, session_id, invocation_context) as terminal_client:
            terminals = await terminal_client.list_resources()
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
                        # 持久记录只承载工作区内相对目录；真实决定路径在 PTY 创建调用栈内解析。
                        "cwd_relative": terminal.get("cwd_relative"),
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
    terminal_owner: TerminalResourceOwner,
    invocation_context: ToolInvocationContext,
) -> BaseTool:
    """创建终止当前 Agent Session 所属终端执行的工具。"""

    owner_session_id = session_id

    @tool("kill_terminal")
    async def kill_terminal(session_id: str) -> dict[str, Any]:
        """终止 exec_command 返回的 session_id 及其完整进程树。"""
        async with _terminal_operation(terminal_owner, owner_session_id, invocation_context) as terminal_client:
            terminal = await terminal_client.get_resource(session_id)
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
