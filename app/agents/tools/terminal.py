from __future__ import annotations

import asyncio
import re
import shlex
import uuid
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool

from app.services.infrastructure.terminal_manager_client import TerminalManagerClient


def _extract_command_output(
    *,
    buffer: str,
    previous_buffer: str,
    start_marker: str,
    done_marker: str,
) -> tuple[bool, str, int | None]:
    if buffer.startswith(previous_buffer):
        new_output = buffer[len(previous_buffer):]
    else:
        new_output = buffer

    normalized_output = new_output.replace("\r\n", "\n").replace("\r", "\n")
    done_matches = list(
        re.finditer(rf"^{re.escape(done_marker)}:(\d+)\s*$", normalized_output, re.MULTILINE)
    )
    if not done_matches:
        return False, new_output, None

    done_match = done_matches[-1]
    start_matches = list(
        re.finditer(
            rf"^{re.escape(start_marker)}\s*$",
            normalized_output[: done_match.start()],
            re.MULTILINE,
        )
    )
    if start_matches:
        output = normalized_output[start_matches[-1].end(): done_match.start()]
    else:
        output = normalized_output[: done_match.start()]
    exit_code = int(done_match.group(1))
    return True, output.strip(), exit_code


def create_persistent_terminal_tool(
    session_id: str,
    agent_id: str = "default",
    *,
    terminal_client: TerminalManagerClient,
) -> BaseTool:
    """创建持久终端工具，命令超时后终端继续留在后台运行。"""

    @tool("persistent_terminal")
    async def persistent_terminal(
        action: Literal["run_command", "write_input", "snapshot", "kill"] = "run_command",
        command: str | None = None,
        terminal_id: str | None = None,
        input_text: str | None = None,
        timeout_seconds: int = 10,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """管理一个可 attach 的持久终端。run_command 超时后不会杀进程，会返回后台终端 attach_url。"""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")

        if action == "snapshot":
            if not terminal_id:
                raise ValueError("snapshot 需要 terminal_id")
            return await terminal_client.get_terminal(terminal_id)

        if action == "kill":
            if not terminal_id:
                raise ValueError("kill 需要 terminal_id")
            return await terminal_client.kill_terminal(terminal_id)

        if action == "write_input":
            if not terminal_id:
                raise ValueError("write_input 需要 terminal_id")
            if input_text is None:
                raise ValueError("write_input 需要 input_text")
            return await terminal_client.write_terminal(
                terminal_id=terminal_id,
                data=input_text,
                source="agent",
            )

        if action != "run_command":
            raise ValueError(f"未知 persistent_terminal action: {action}")
        if not command or not command.strip():
            raise ValueError("run_command 需要 command")

        if terminal_id:
            terminal = await terminal_client.get_terminal(terminal_id)
        else:
            existing_terminals = terminal_client.list_terminals_from_state(session_id)
            terminal = next(
                (
                    existing
                    for existing in existing_terminals
                    if existing.get("status") == "running"
                ),
                None,
            )
            if terminal is None:
                terminal = await terminal_client.create_terminal(
                    session_id=session_id,
                    title=f"{agent_id} terminal",
                    cwd=cwd,
                )
            terminal_id = str(terminal["terminal_id"])

        previous_buffer = str(terminal.get("buffer") or "")
        run_id = uuid.uuid4().hex[:12]
        start_marker = f"__BOXTEAM_CMD_START_{run_id}__"
        done_marker = f"__BOXTEAM_CMD_DONE_{run_id}__"
        wrapped_command = (
            f"printf '\\n{start_marker}\\n'; "
            f"bash -lc {shlex.quote(command)}; "
            f"__boxteam_rc=$?; "
            f"printf '\\n{done_marker}:%s\\n' \"$__boxteam_rc\"\r"
        )
        await terminal_client.write_terminal(
            terminal_id=terminal_id,
            data=wrapped_command,
            source="agent",
            command=command,
        )

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        latest_terminal = terminal
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            latest_terminal = await terminal_client.get_terminal(terminal_id)
            completed, output, exit_code = _extract_command_output(
                buffer=str(latest_terminal.get("buffer") or ""),
                previous_buffer=previous_buffer,
                start_marker=start_marker,
                done_marker=done_marker,
            )
            if completed:
                attach_url = terminal_client.attach_url(terminal_id)
                return {
                    "status": "completed",
                    "terminal_id": terminal_id,
                    "session_id": session_id,
                    "command": command,
                    "exit_code": exit_code,
                    "output": output,
                    "display_summary": (
                        f"终端 ID: {terminal_id}\n"
                        f"会话 ID: {session_id}\n"
                        f"命令状态: 已完成，退出码 {exit_code}\n"
                        f"终端链接: {attach_url}\n"
                        "使用方式: 打开终端网页后，可在页面底部输入框继续发送命令。"
                    ),
                    "message": (
                        "命令已完成，终端会话仍保留，可从资源视图打开，"
                        f"也可访问 {attach_url}。打开终端网页后，可在页面底部输入框继续发送命令。"
                    ),
                    "attach_url": attach_url,
                }

        completed, output, _ = _extract_command_output(
            buffer=str(latest_terminal.get("buffer") or ""),
            previous_buffer=previous_buffer,
            start_marker=start_marker,
            done_marker=done_marker,
        )
        if completed:
            raise RuntimeError("终端命令完成状态解析出现不一致，请重试 snapshot 查看终端状态")

        return {
            "status": "background",
            "terminal_id": terminal_id,
            "session_id": session_id,
            "command": command,
            "display_summary": (
                f"终端 ID: {terminal_id}\n"
                f"会话 ID: {session_id}\n"
                "命令状态: 仍在后台运行\n"
                f"终端链接: {terminal_client.attach_url(terminal_id)}\n"
                "使用方式: 打开终端网页后，可在页面底部输入框继续发送命令。"
            ),
            "message": (
                "命令仍在运行，已保留为可 attach 的后台终端，"
                f"可访问 {terminal_client.attach_url(terminal_id)}。"
                "打开终端网页后，可在页面底部输入框继续发送命令。"
            ),
            "recent_output": output,
            "attach_url": terminal_client.attach_url(terminal_id),
            "terminal": latest_terminal,
        }

    return persistent_terminal
