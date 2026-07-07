from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from app.core.background_task_registry import BackgroundTaskHandle


@runtime_checkable
class TerminalManagerClientProtocol(Protocol):
    def attach_url(self, terminal_id: str) -> str: ...

    def list_terminals_from_state(self, session_id: str) -> list[dict[str, object]]: ...

    async def kill_terminal(self, terminal_id: str) -> dict[str, object]: ...

    async def delete_terminal(self, terminal_id: str) -> dict[str, object]: ...


@runtime_checkable
class HistoricalTerminalRecordReaderProtocol(Protocol):
    def read_records(
        self,
        *,
        session_id: str,
        active_terminals: Sequence[Mapping[str, object]],
        agent_state_records: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]: ...


@runtime_checkable
class BackgroundTaskRegistryProtocol(Protocol):
    def list_handles(self, session_id: str) -> list[BackgroundTaskHandle]: ...

    def get_handle(self, session_id: str, task_id: str) -> BackgroundTaskHandle | None: ...

    async def cancel(self, session_id: str, task_id: str) -> BackgroundTaskHandle: ...

    async def delete(self, session_id: str, task_id: str) -> BackgroundTaskHandle: ...

    async def delete_session(self, session_id: str) -> int: ...


@runtime_checkable
class SessionResourceMessageProtocol(Protocol):
    async def list_agent_state_records(
        self,
        session_id: str,
        *,
        strict: bool = False,
    ) -> list[dict[str, object]]: ...

    def append_system_reminder(
        self,
        *,
        session_id: str,
        reminder: str,
        response_metadata: dict[str, object],
        checkpoint_source: str,
        assistant_text: str = "",
        assistant_response_metadata: dict[str, object] | None = None,
    ) -> bool: ...
