"""Terminal 领域 owner 与 PTY 传输进程之间的单一边界。"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol

from app.abstractions.workspace_resources import ResourceTransport

TerminalSnapshot = dict[str, object]


class TerminalWorkingDirectories(Protocol):
    def admit_working_directory(self, target: Path) -> AbstractAsyncContextManager[str | None]:
        """保护目录准入，直到调用方发布持久进程占用；返回受管目录 URI。"""
        ...


class TerminalRequestError(RuntimeError):
    """PTY 服务的 HTTP 失败；响应正文只供诊断，不能投影为业务错误。"""

    def __init__(self, *, method: str, path: str, status: int, response_body: str) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.response_body = response_body
        super().__init__(
            f"终端管理器请求失败: method={method}, status={status}；请查看工作区终端服务日志"
        )


class TerminalTransport(ResourceTransport, Protocol):
    async def create_terminal(
        self, *, terminal_id: str, owner_revision: int, title: str, cwd: str,
        agent_id: str | None = None,
        cols: int = 100, rows: int = 30,
    ) -> TerminalSnapshot: ...

    async def read_terminal(self, terminal_id: str) -> TerminalSnapshot: ...

    async def write_terminal(
        self, *, terminal_id: str, data: str, source: str = "agent",
        command: str | None = None,
    ) -> TerminalSnapshot: ...

    async def mark_terminal_backgrounded(self, terminal_id: str) -> TerminalSnapshot: ...

    async def claim_terminal_steering(
        self, terminal_id: str, *, completion_event_id: str, command_start_sequence: int,
    ) -> TerminalSnapshot: ...

    async def finish_terminal_steering(
        self, terminal_id: str, *, completion_event_id: str, command_start_sequence: int,
        dispatched: bool,
    ) -> TerminalSnapshot: ...
