"""后台任务创建、查询与显式生命周期管理端口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.background_task_contracts import (
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
)


@runtime_checkable
class BackgroundTaskRegistryProtocol(Protocol):
    async def spawn(
        self, *, session_id: str, thread_id: str, agent_id: str, task_id: str,
        descriptor: BackgroundTaskDescriptor,
    ) -> BackgroundTaskHandle: ...

    def list_handles(self, session_id: str) -> list[BackgroundTaskHandle]: ...

    def list_closed_handles(self, session_id: str) -> list[BackgroundTaskHandle]: ...

    def get_handle(self, session_id: str, task_id: str) -> BackgroundTaskHandle | None: ...

    async def cancel(self, session_id: str, task_id: str) -> BackgroundTaskHandle: ...

    async def reconcile(self, session_id: str, task_id: str) -> BackgroundTaskHandle: ...

    async def delete(self, session_id: str, task_id: str) -> BackgroundTaskHandle: ...

    async def delete_session(self, session_id: str) -> int: ...
