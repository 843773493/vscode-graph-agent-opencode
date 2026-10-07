"""Terminal 领域 owner 与 PTY 传输进程之间的单一边界。"""

from __future__ import annotations

from typing import Protocol

from app.abstractions.workspace_resources import ResourceTransport

TerminalSnapshot = dict[str, object]


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
