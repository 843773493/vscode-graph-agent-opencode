"""后台任务创建、查询与显式生命周期管理端口。"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import JsonValue

from app.domain.background_work import (
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskStep,
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


class BackgroundTaskHandler(Protocol):
    """每个版本必须校验 descriptor/checkpoint，明确执行副作用能否重放。"""

    kind: str
    version: int
    @property
    def effect(self) -> Literal["atomic_mailbox", "external"]: ...

    def initial_checkpoint(self, descriptor: BackgroundTaskDescriptor) -> dict[str, JsonValue]: ...

    def validate_checkpoint(self, checkpoint: dict[str, JsonValue]) -> None: ...

    async def step(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep: ...

    async def reconcile(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep | None: ...
