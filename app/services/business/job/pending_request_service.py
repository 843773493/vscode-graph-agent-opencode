from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.abstractions.job_executor import JobInputOwnerProtocol
from app.abstractions.pending_request_store import PendingRequestStoreProtocol
from app.schemas.internal_v2.pending_request import (
    DeliveryPolicy,
    PendingRequestDTO,
    PendingRequestListDTO,
    PendingRequestSummaryListDTO,
)
from app.services.business.job.pending_request_controller import (
    JobPendingRequestController,
    PendingJob,
)


class JobPendingRequestService:
    """聚合 FIFO 队列状态控制与持久化。"""

    def __init__(
        self,
        *,
        queue,
        lock: asyncio.Lock,
        store: PendingRequestStoreProtocol | None,
        get_jobs: Callable[[], dict[str, PendingJob]],
        get_current_jobs: Callable[[], dict[tuple[str, str], str]],
        persist_terminal_status: Callable[[PendingJob, str], Awaitable[bool]],
        input_owner: JobInputOwnerProtocol | None,
    ) -> None:
        self._store = store
        self._store_lock = asyncio.Lock()
        # 只记录进程内恢复阶段；队列及执行事实仍由原 owner 保存。
        self._loaded_sessions: dict[str, bool] = {}
        self._controller = JobPendingRequestController(
            queue=queue,
            lock=lock,
            get_jobs=get_jobs,
            get_current_jobs=get_current_jobs,
            persist_terminal_status=persist_terminal_status,
            input_owner=input_owner,
        )

    async def load_unloaded(self, session_id: str) -> list[PendingRequestDTO] | None:
        if session_id in self._loaded_sessions:
            return None
        return await self._store.load(session_id) if self._store is not None else []

    def mark_loaded(self, session_id: str) -> None:
        """装载后不再覆盖内存队列；落盘与派发完成前仍需重试恢复。"""
        self._loaded_sessions[session_id] = False

    def mark_restored(self, session_id: str) -> None:
        self._loaded_sessions[session_id] = True

    def is_restored(self, session_id: str) -> bool:
        return self._loaded_sessions.get(session_id, False)

    async def list(
        self, session_id: str, *, thread_id: str | None = None,
    ) -> PendingRequestListDTO:
        return await self._controller.list(session_id, thread_id=thread_id)

    async def list_summaries(
        self,
        session_id: str,
        *,
        limit: int,
        thread_id: str | None = None,
    ) -> PendingRequestSummaryListDTO:
        if session_id in self._loaded_sessions or self._store is None:
            return await self._controller.list_summaries(session_id, limit=limit, thread_id=thread_id)
        return await self._store.load_summaries(session_id, limit=limit, thread_id=thread_id)

    async def update_policy(
        self,
        session_id: str,
        message_id: str,
        *,
        delivery_policy: DeliveryPolicy,
        expected_snapshot_version: int | None,
    ) -> PendingRequestListDTO:
        snapshot = await self._controller.update_policy(
            session_id,
            message_id,
            delivery_policy=delivery_policy,
            expected_snapshot_version=expected_snapshot_version,
        )
        await self.persist(snapshot)
        return snapshot

    async def remove(self, session_id: str, message_id: str) -> PendingRequestListDTO:
        snapshot = await self._controller.remove(session_id, message_id)
        await self.persist(snapshot)
        return snapshot

    async def clear(self, session_id: str) -> PendingRequestListDTO:
        try:
            return await self._controller.clear(session_id)
        finally:
            # 多个 thread 的终态各自提交；中途失败也保留已经撤回的队列结果。
            await self.persist_current(session_id)

    async def reject_reorder(self, session_id: str) -> None:
        await self._controller.reject_reorder(session_id)

    async def persist(self, snapshot: PendingRequestListDTO) -> None:
        if self._store is None:
            return
        async with self._store_lock:
            latest = await self._controller.list(snapshot.session_id, display=False)
            await self._store.save(latest.session_id, list(latest.requests))

    async def persist_current(self, session_id: str) -> None:
        """把当前内存队列写回磁盘；未配置持久化时不构造展示 DTO。"""
        if self._store is None:
            return
        await self.persist(await self.list(session_id))

    async def delete(self, session_id: str) -> None:
        self._loaded_sessions.pop(session_id, None)
        if self._store is not None:
            await self._store.delete(session_id)
