from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from app.abstractions.workspace_resources import ResourceChanges


class TerminalMonitorOperation(Protocol):
    async def claim_terminal_steering(
        self, terminal_id: str, *, completion_event_id: str, command_start_sequence: int,
    ) -> dict[str, object]: ...

    async def finish_terminal_steering(
        self, terminal_id: str, *, completion_event_id: str, command_start_sequence: int,
        dispatched: bool,
    ) -> dict[str, object]: ...


class TerminalExecutionMonitorClientProtocol(Protocol):
    def runtime_changes(self) -> AsyncIterator[ResourceChanges]: ...

    def operation(
        self, *, session_id: str, thread_id: str, holder_id: str, operation_id: str,
    ) -> AbstractAsyncContextManager[TerminalMonitorOperation]: ...

    async def sweep_orphans(self) -> list[dict[str, object]]: ...
    async def list_resources(
        self,
        *,
        session_id: str | None = None,
        resource_ids: frozenset[str] | None = None,
    ) -> list[dict[str, object]]: ...

__all__ = ["TerminalExecutionMonitorClientProtocol"]
