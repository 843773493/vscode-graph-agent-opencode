from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from app.schemas.public_v2.common import JobStatus
from app.schemas.public_v2.message import AttachmentRef
from app.schemas.public_v2.pending_request import (
    PendingRequestDTO,
    PendingRequestKind,
    PendingRequestListDTO,
    PendingRequestOrderItem,
)
from app.services.business.job.pending_queue import JobPendingQueue


class PendingJob(Protocol):
    job_id: str
    message_id: str
    session_id: str
    message: str
    agent_id: str
    message_created_at: str
    message_metadata: dict[str, object]
    attachments: list[AttachmentRef]
    pending_kind: PendingRequestKind | None
    status: JobStatus
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None


class JobPendingRequestController:
    """实现待处理消息的查询和用户控制，不负责 Job 执行调度。"""

    def __init__(
        self,
        *,
        queue: JobPendingQueue,
        lock: asyncio.Lock,
        get_jobs: Callable[[], dict[str, PendingJob]],
        get_current_jobs: Callable[[], dict[str, str]],
    ) -> None:
        self._queue = queue
        self._lock = lock
        self._get_jobs = get_jobs
        self._get_current_jobs = get_current_jobs

    def _job_by_message_id(
        self,
        session_id: str,
        message_id: str,
    ) -> PendingJob:
        jobs = self._get_jobs()
        for job_id in self._queue.ids(session_id):
            job = jobs.get(job_id)
            if job is not None and job.message_id == message_id:
                return job
        raise ValueError(f"Session {session_id} 中不存在待处理消息 {message_id}")

    @staticmethod
    def _dto(job: PendingJob, position: int) -> PendingRequestDTO:
        if job.pending_kind is None:
            raise RuntimeError(f"待处理 Job 缺少 kind: job_id={job.job_id}")
        return PendingRequestDTO(
            job_id=job.job_id,
            message_id=job.message_id,
            session_id=job.session_id,
            content=job.message,
            attachments=list(job.attachments),
            kind=job.pending_kind,
            position=position,
            agent_id=job.agent_id,
            message_created_at=job.message_created_at,
            message_metadata=dict(job.message_metadata),
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    async def list(self, session_id: str) -> PendingRequestListDTO:
        async with self._lock:
            return self._snapshot_unlocked(session_id)

    def _snapshot_unlocked(self, session_id: str) -> PendingRequestListDTO:
        jobs = self._get_jobs()
        requests = [
            self._dto(jobs[job_id], position)
            for position, job_id in enumerate(self._queue.ids(session_id))
            if job_id in jobs
        ]
        return PendingRequestListDTO(
            session_id=session_id,
            active_job_id=self._get_current_jobs().get(session_id),
            yield_requested=self._queue.yield_requested(session_id),
            requests=requests,
        )

    async def update(
        self,
        session_id: str,
        message_id: str,
        *,
        content: str,
        attachments: list[AttachmentRef],
    ) -> PendingRequestListDTO:
        normalized_content = content.strip()
        if not normalized_content and not attachments:
            raise ValueError("待处理消息正文和附件不能同时为空")
        async with self._lock:
            job = self._job_by_message_id(session_id, message_id)
            job.message = normalized_content
            job.attachments = list(attachments)
            job.updated_at = datetime.now()
        return await self.list(session_id)

    async def remove(
        self,
        session_id: str,
        message_id: str,
    ) -> PendingRequestListDTO:
        async with self._lock:
            job = self._job_by_message_id(session_id, message_id)
            if not self._queue.remove(session_id, job.job_id):
                raise RuntimeError(
                    f"撤回待处理消息时队列状态不一致: job_id={job.job_id}"
                )
            job.status = JobStatus.cancelled
            job.error_message = "消息已从队列撤回"
            job.ended_at = datetime.now()
            job.updated_at = job.ended_at
        return await self.list(session_id)

    async def clear(self, session_id: str) -> PendingRequestListDTO:
        async with self._lock:
            removed_ids = self._queue.clear(session_id)
            jobs = self._get_jobs()
            now = datetime.now()
            for job_id in removed_ids:
                job = jobs.get(job_id)
                if job is None:
                    continue
                job.status = JobStatus.cancelled
                job.error_message = "消息已从队列撤回"
                job.ended_at = now
                job.updated_at = now
        return await self.list(session_id)

    async def reorder(
        self,
        session_id: str,
        requests: list[PendingRequestOrderItem],
    ) -> PendingRequestListDTO:
        async with self._lock:
            all_jobs = self._get_jobs()
            jobs = [
                all_jobs[job_id]
                for job_id in self._queue.ids(session_id)
                if job_id in all_jobs
            ]
            self._queue.reorder(
                session_id,
                requests,
                job_id_by_message_id={
                    job.message_id: job.job_id for job in jobs
                },
            )
            kind_by_message_id = {
                item.message_id: item.kind for item in requests
            }
            for job in jobs:
                job.pending_kind = kind_by_message_id[job.message_id]
                job.updated_at = datetime.now()
        return await self.list(session_id)

    async def promote_and_reserve_if_idle(
        self,
        session_id: str,
        message_id: str,
        reservation: PendingJob,
    ) -> tuple[PendingRequestListDTO, bool]:
        async with self._lock:
            target = self._job_by_message_id(session_id, message_id)
            self._queue.promote(session_id, target.job_id)
            current_jobs = self._get_current_jobs()
            reserved = current_jobs.get(session_id) is None
            if reserved:
                self._get_jobs()[reservation.job_id] = reservation
                current_jobs[session_id] = reservation.job_id
            return self._snapshot_unlocked(session_id), reserved

    async def release_reservation(
        self,
        session_id: str,
        reservation_job_id: str,
    ) -> None:
        async with self._lock:
            current_jobs = self._get_current_jobs()
            if current_jobs.get(session_id) == reservation_job_id:
                current_jobs.pop(session_id, None)
            self._get_jobs().pop(reservation_job_id, None)
