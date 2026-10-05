from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol


class TerminalMonitorOperation(Protocol):
    async def claim_terminal_steering(self, terminal_id: str) -> dict[str, object]: ...

    async def finish_terminal_steering(
        self, terminal_id: str, *, dispatched: bool,
    ) -> dict[str, object]: ...


class TerminalExecutionMonitorClientProtocol(Protocol):
    def operation(
        self, *, session_id: str, thread_id: str, holder_id: str, operation_id: str,
    ) -> AbstractAsyncContextManager[TerminalMonitorOperation]: ...

    async def sweep_orphans(self) -> list[dict[str, object]]: ...
    async def list_resources(
        self,
        *,
        session_id: str | None = None,
    ) -> list[dict[str, object]]: ...

__all__ = ["TerminalExecutionMonitorClientProtocol"]
