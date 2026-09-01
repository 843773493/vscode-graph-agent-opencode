from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.abstractions.pending_request_store import PendingRequestStoreProtocol
from app.schemas.internal_v2.message import AttachmentRef
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
        get_current_jobs: Callable[[], dict[str, str]],
    ) -> None:
        self._store = store
        self._store_lock = asyncio.Lock()
        self._loaded_sessions: set[str] = set()
        self._controller = JobPendingRequestController(
            queue=queue,
            lock=lock,
            get_jobs=get_jobs,
            get_current_jobs=get_current_jobs,
        )

    async def load_once(self, session_id: str) -> list[PendingRequestDTO]:
        if session_id in self._loaded_sessions:
            return []
        records = await self._store.load(session_id) if self._store is not None else []
        self._loaded_sessions.add(session_id)
        return records

    async def list(self, session_id: str) -> PendingRequestListDTO:
        return await self._controller.list(session_id)

    async def list_summaries(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> PendingRequestSummaryListDTO:
        if session_id in self._loaded_sessions or self._store is None:
            return await self._controller.list_summaries(session_id, limit=limit)
        return await self._store.load_summaries(session_id, limit=limit)

    async def update(
        self,
        session_id: str,
        message_id: str,
        *,
        content: str,
        attachments: list[AttachmentRef],
    ) -> PendingRequestListDTO:
        snapshot = await self._controller.update(
            session_id,
            message_id,
            content=content,
            attachments=attachments,
        )
        await self.persist(snapshot)
        return snapshot

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
        snapshot = await self._controller.clear(session_id)
        await self.persist(snapshot)
        return snapshot

    async def reject_reorder(self, session_id: str) -> None:
        await self._controller.reject_reorder(session_id)

    async def persist(self, snapshot: PendingRequestListDTO) -> None:
        if self._store is None:
            return
        async with self._store_lock:
            latest = await self._controller.list(snapshot.session_id)
            await self._store.save(latest.session_id, list(latest.requests))

    async def persist_current(self, session_id: str) -> None:
        """把当前内存队列写回磁盘；未配置持久化时不构造展示 DTO。"""
        if self._store is None:
            return
        await self.persist(await self.list(session_id))

    async def delete(self, session_id: str) -> None:
        self._loaded_sessions.discard(session_id)
        if self._store is not None:
            await self._store.delete(session_id)
