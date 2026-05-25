from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest


@dataclass
class ShellSession:
    session_id: str
    shell: str
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    closed: bool = False

    def execute(self, command: str) -> str:
        if self.closed:
            raise RuntimeError("shell session 已关闭")
        if not command.strip():
            raise ValueError("command 不能为空")

        self.history.append(command)

        # 最小原型：先只验证“持续会话的状态承载能力”
        # 后续真正接 node-pty 时，这里会替换为长期 PTY 进程读写。
        return f"[{self.shell}] cwd={self.cwd} command={command}"

    def close(self) -> None:
        self.closed = True


class ShellSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, ShellSession] = {}

    def start(self, session_id: str, shell: str, cwd: str | Path, env: dict[str, str] | None = None) -> ShellSession:
        if not session_id:
            raise ValueError("session_id 不能为空")
        if not shell:
            raise ValueError("shell 不能为空")

        session = ShellSession(
            session_id=session_id,
            shell=shell,
            cwd=Path(cwd),
            env=dict(env or {}),
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> ShellSession:
        return self._sessions[session_id]

    def stop(self, session_id: str) -> None:
        session = self._sessions.pop(session_id)
        session.close()


@pytest.mark.asyncio
async def test_shell_session_manager_can_keep_shell_identity_and_cwd(tmp_path: Path) -> None:
    manager = ShellSessionManager()
    session = manager.start(
        session_id="ses_demo",
        shell="pwsh",
        cwd=tmp_path,
        env={"DEMO_FLAG": "1"},
    )

    assert session.session_id == "ses_demo"
    assert session.shell == "pwsh"
    assert session.cwd == tmp_path
    assert session.env["DEMO_FLAG"] == "1"
    assert session.history == []

    first = session.execute("Get-Location")
    second = session.execute("pwd")

    assert "[pwsh]" in first
    assert "cwd=" in first
    assert session.history == ["Get-Location", "pwd"]
    assert "pwd" in second

    manager.stop("ses_demo")
    with pytest.raises(RuntimeError, match="已关闭"):
        session.execute("echo after close")


@pytest.mark.asyncio
async def test_shell_session_manager_reuses_session_object_by_id(tmp_path: Path) -> None:
    manager = ShellSessionManager()
    created = manager.start("ses_reuse", "bash", tmp_path)
    fetched = manager.get("ses_reuse")

    assert fetched is created
    fetched.execute("echo hello")
    assert manager.get("ses_reuse").history == ["echo hello"]


@pytest.mark.asyncio
async def test_shell_session_manager_rejects_invalid_inputs(tmp_path: Path) -> None:
    manager = ShellSessionManager()

    with pytest.raises(ValueError, match="session_id 不能为空"):
        manager.start("", "pwsh", tmp_path)

    with pytest.raises(ValueError, match="shell 不能为空"):
        manager.start("ses_invalid", "", tmp_path)

    session = manager.start("ses_empty_command", "cmd", tmp_path)
    with pytest.raises(ValueError, match="command 不能为空"):
        session.execute("   ")