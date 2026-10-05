from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.background_task_registry import BackgroundTaskHandle
from app.schemas.internal_v2.session_resource import (
    SessionResourceAction,
    SessionResourceControlResultDTO,
    SessionResourceDTO,
    SessionResourceKind,
)


@runtime_checkable
class BackgroundTaskRegistryProtocol(Protocol):
    def list_handles(self, session_id: str) -> list[BackgroundTaskHandle]: ...

    def list_closed_handles(self, session_id: str) -> list[BackgroundTaskHandle]: ...

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

    def submit_system_reminder(
        self,
        *,
        session_id: str,
        reminder: str,
        response_metadata: dict[str, object],
        checkpoint_source: str,
        event_identity: str | None = None,
    ) -> bool: ...


@runtime_checkable
class SessionResourceProviderProtocol(Protocol):
    """一种会话后台资源的显式生命周期提供者。"""

    kind: SessionResourceKind

    async def list_resources(
        self,
        session_id: str,
        *,
        include_history: bool = True,
    ) -> list[SessionResourceDTO]: ...

    async def control(
        self,
        *,
        session_id: str,
        resource_id: str,
        action: SessionResourceAction,
        operation_id: str,
    ) -> SessionResourceControlResultDTO: ...

    async def cleanup_session(self, session_id: str) -> int: ...
