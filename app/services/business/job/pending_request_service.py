from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.abstractions.execution_queue import ExecutionQueueProtocol
from app.abstractions.job_executor import JobInputOwnerProtocol
from app.schemas.internal_v2.pending_request import (
    DeliveryPolicy,
    PendingRequestDTO,
    PendingRequestListDTO,
    PendingRequestSummaryListDTO,
)
from app.services.business.job.pending_queue import JobPendingQueue
from app.services.business.job.pending_request_controller import (
    JobPendingRequestController,
    PendingJob,
)


class JobPendingRequestService:
    """协调内存待处理投影与持久 Thread 执行队列。"""

    def __init__(
        self,
        *,
        pending_queue: JobPendingQueue,
        lock: asyncio.Lock,
        execution_queue: ExecutionQueueProtocol | None,
        get_jobs: Callable[[], dict[str, PendingJob]],
        get_current_jobs: Callable[[], dict[tuple[str, str], str]],
        persist_terminal_status: Callable[[PendingJob, str], Awaitable[bool]],
        input_owner: JobInputOwnerProtocol | None,
    ) -> None:
        self._execution_queue = execution_queue
        self._pending_queue = pending_queue
        # 只区分可否使用进程内摘要投影；恢复阶段以持久 claim 为准。
        self._loaded_sessions: set[str] = set()
        self._controller = JobPendingRequestController(
            pending_queue=pending_queue,
            lock=lock,
            get_jobs=get_jobs,
            get_current_jobs=get_current_jobs,
            persist_terminal_status=persist_terminal_status,
            input_owner=input_owner, execution_queue=execution_queue,
        )

    async def load(self, session_id: str) -> list[PendingRequestDTO]:
        return await self._execution_queue.load(session_id) if self._execution_queue is not None else []

    def mark_loaded(self, session_id: str) -> None:
        self._loaded_sessions.add(session_id)

    async def list(
        self, session_id: str, *, thread_id: str | None = None,
    ) -> PendingRequestListDTO:
        await self.refresh_version(session_id)
        return await self._controller.list(session_id, thread_id=thread_id)

    async def list_summaries(
        self,
        session_id: str,
        *,
        limit: int,
        thread_id: str | None = None,
    ) -> PendingRequestSummaryListDTO:
        if session_id in self._loaded_sessions or self._execution_queue is None:
            return await self._controller.list_summaries(session_id, limit=limit, thread_id=thread_id)
        return await self._execution_queue.load_summaries(session_id, limit=limit, thread_id=thread_id)

    async def update_policy(
        self,
        session_id: str,
        message_id: str,
        *,
        delivery_policy: DeliveryPolicy,
        expected_snapshot_version: int | None,
    ) -> PendingRequestListDTO:
        await self._controller.update_policy(
            session_id,
            message_id,
            delivery_policy=delivery_policy,
            expected_snapshot_version=expected_snapshot_version,
        )
        return await self.list(session_id)

    async def remove(self, session_id: str, message_id: str) -> PendingRequestListDTO:
        await self._controller.remove(session_id, message_id)
        return await self.list(session_id)

    async def clear(self, session_id: str, *, thread_id: str | None = None) -> PendingRequestListDTO:
        try:
            await self._controller.clear(session_id, thread_id=thread_id)
        finally:
            # 多个 thread 的终态各自提交；中途失败也保留已经撤回的队列结果。
            await self.refresh_version(session_id)
        return await self.list(session_id, thread_id=thread_id)

    @property
    def execution_queue(self) -> ExecutionQueueProtocol:
        if self._execution_queue is None:
            raise RuntimeError("Job 调度缺少持久 Thread 执行队列 owner")
        return self._execution_queue

    async def refresh_version(self, session_id: str) -> None:
        if self._execution_queue is not None:
            self._pending_queue.set_snapshot_version(session_id, await self._execution_queue.snapshot_version(session_id))

    async def delete(self, session_id: str) -> None:
        self._loaded_sessions.discard(session_id)
