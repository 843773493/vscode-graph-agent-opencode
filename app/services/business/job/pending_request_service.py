from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.abstractions.pending_request_store import PendingRequestStoreProtocol
from app.schemas.public_v2.message import AttachmentRef
from app.schemas.public_v2.pending_request import (
    PendingRequestDTO,
    PendingRequestListDTO,
    PendingRequestOrderItem,
)
from app.services.business.job.pending_queue import JobPendingQueue
from app.services.business.job.pending_request_controller import (
    JobPendingRequestController,
    PendingJob,
)


class JobPendingRequestService:
    """聚合待处理请求的状态控制与持久化，不参与 Job 执行。"""

    def __init__(
        self,
        *,
        queue: JobPendingQueue,
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

    async def remove(
        self,
        session_id: str,
        message_id: str,
    ) -> PendingRequestListDTO:
        snapshot = await self._controller.remove(session_id, message_id)
        await self.persist(snapshot)
        return snapshot

    async def clear(self, session_id: str) -> PendingRequestListDTO:
        snapshot = await self._controller.clear(session_id)
        await self.persist(snapshot)
        return snapshot

    async def reorder(
        self,
        session_id: str,
        requests: list[PendingRequestOrderItem],
    ) -> PendingRequestListDTO:
        snapshot = await self._controller.reorder(session_id, requests)
        await self.persist(snapshot)
        return snapshot

    async def promote_and_reserve_if_idle(
        self,
        session_id: str,
        message_id: str,
        reservation: PendingJob,
    ) -> tuple[PendingRequestListDTO, bool]:
        snapshot, reserved = await self._controller.promote_and_reserve_if_idle(
            session_id,
            message_id,
            reservation,
        )
        try:
            await self.persist(snapshot)
        except BaseException:
            if reserved:
                await self._controller.release_reservation(
                    session_id,
                    reservation.job_id,
                )
            raise
        return snapshot, reserved

    async def persist(self, snapshot: PendingRequestListDTO) -> None:
        if self._store is None:
            return
        async with self._store_lock:
            latest = await self._controller.list(snapshot.session_id)
            await self._store.save(latest.session_id, list(latest.requests))

    async def delete(self, session_id: str) -> None:
        self._loaded_sessions.discard(session_id)
        if self._store is not None:
            await self._store.delete(session_id)
