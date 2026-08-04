from __future__ import annotations

from typing import Protocol


class TerminalExecutionMonitorClientProtocol(Protocol):
    async def list_terminals(
        self,
        *,
        session_id: str | None = None,
    ) -> list[dict[str, object]]: ...

    async def claim_terminal_steering(
        self,
        terminal_id: str,
    ) -> dict[str, object]: ...

    async def finish_terminal_steering(
        self,
        terminal_id: str,
        *,
        dispatched: bool,
    ) -> dict[str, object]: ...


__all__ = ["TerminalExecutionMonitorClientProtocol"]
