"""后台任务创建、查询与显式生命周期管理端口。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import JsonValue

from app.domain.background_work import (
    BackgroundTaskDescriptor,
    BackgroundTaskHandle,
    BackgroundTaskStep,
    BackgroundTaskSummary,
)

BackgroundTaskDescriptorFactory = Callable[
    [BackgroundTaskHandle | None], AbstractAsyncContextManager[BackgroundTaskDescriptor]
]


@runtime_checkable
class BackgroundTaskRegistryProtocol(Protocol):
    async def spawn(
        self, *, session_id: str, thread_id: str, agent_id: str, task_id: str,
        descriptor: BackgroundTaskDescriptor,
    ) -> BackgroundTaskHandle: ...

    async def spawn_derived(
        self, *, session_id: str, thread_id: str, agent_id: str, task_id: str,
        descriptor: BackgroundTaskDescriptor | BackgroundTaskDescriptorFactory, foreground: bool = False,
    ) -> BackgroundTaskHandle | None: ...

    async def recover_derived_thread(self, session_id: str, thread_id: str) -> tuple[BackgroundTaskHandle, ...]: ...

    async def promote_derived(self, session_id: str, task_id: str) -> BackgroundTaskHandle: ...

    async def retry_derived(
        self, session_id: str, task_id: str, *, deadline: datetime, validate: Callable[[BackgroundTaskHandle], None],
    ) -> BackgroundTaskHandle: ...

    def derived_publication(self, handle: BackgroundTaskHandle) -> AbstractAsyncContextManager[None]: ...

    def list_handles(self, session_id: str) -> list[BackgroundTaskHandle]: ...

    def list_summaries(self, session_id: str, *, include_history: bool) -> list[BackgroundTaskSummary]: ...

    def get_handle(self, session_id: str, task_id: str) -> BackgroundTaskHandle | None: ...

    async def cancel(self, session_id: str, task_id: str) -> BackgroundTaskHandle: ...

    async def reconcile(self, session_id: str, task_id: str) -> BackgroundTaskHandle: ...

    async def delete(self, session_id: str, task_id: str) -> BackgroundTaskHandle: ...

    async def cleanup_session(
        self, session_id: str, *, action: Literal["stop", "delete"], thread_id: str | None = None,
    ) -> int: ...


class BackgroundTaskHandler(Protocol):
    """每个版本必须校验 descriptor/checkpoint，明确执行副作用能否重放。"""

    kind: str
    version: int
    @property
    def effect(self) -> Literal["atomic_mailbox", "external", "derived_request"]: ...

    def initial_checkpoint(self, descriptor: BackgroundTaskDescriptor) -> dict[str, JsonValue]: ...

    def validate_checkpoint(self, checkpoint: dict[str, JsonValue]) -> None: ...

    async def step(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep: ...

    async def reconcile(self, handle: BackgroundTaskHandle) -> BackgroundTaskStep | None:
        """derived_request 只查询已提交完整结果；不得重发上游或发布普通消息。"""
        ...


class DerivedBackgroundTaskHandler(BackgroundTaskHandler, Protocol):
    def deadline(self, handle: BackgroundTaskHandle) -> datetime:
        """读取当前 attempt 的固定绝对期限；查询和在途提升不得重置。"""
        ...

    async def abandon(self, handle: BackgroundTaskHandle, *, outcome: Literal["failed", "cancelled"]) -> None:
        """完整结果不存在时，在终态提交前释放本业务的来源保留。"""
        ...
