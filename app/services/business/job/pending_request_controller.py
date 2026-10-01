from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import AttachmentRef
from app.schemas.internal_v2.pending_request import (
    DeliveryPolicy,
    PendingRequestDTO,
    PendingRequestListDTO,
    PendingRequestSummaryDTO,
    PendingRequestSummaryListDTO,
)
from app.services.business.job.lifecycle import transition_job_status
from app.services.business.job.pending_queue import JobPendingQueue
from app.services.business.message_display import project_message_for_display


class PendingJob(Protocol):
    job_id: str
    message_id: str
    session_id: str
    message: str
    agent_id: str
    message_created_at: str
    message_metadata: dict[str, object]
    attachments: list[AttachmentRef]
    delivery_policy: DeliveryPolicy | None
    status: JobStatus
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None


class JobPendingRequestController:
    """实现待处理消息的查询和控制，不负责模型执行。"""

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

    def _job_by_message_id(self, session_id: str, message_id: str) -> PendingJob:
        jobs = self._get_jobs()
        for job_id in self._queue.ids(session_id):
            job = jobs.get(job_id)
            if job is not None and job.message_id == message_id:
                return job
        raise ValueError(f"Session {session_id} 中不存在待处理消息 {message_id}")

    def _dto(self, job: PendingJob, position: int) -> PendingRequestDTO:
        entry = self._queue.entry(job.job_id)
        if job.delivery_policy is None:
            raise RuntimeError(f"待处理 Job 缺少 delivery_policy: job_id={job.job_id}")
        display_projection = project_message_for_display(
            job.message,
            job.message_metadata,
        )
        return PendingRequestDTO(
            job_id=job.job_id,
            message_id=job.message_id,
            session_id=job.session_id,
            content=display_projection.content,
            attachments=list(job.attachments),
            delivery_policy=entry.delivery_policy,
            enqueue_sequence=entry.enqueue_sequence,
            position=position,
            status="queued",
            waiting_reason=entry.waiting_reason,
            last_boundary=entry.last_boundary,
            agent_id=job.agent_id,
            message_created_at=job.message_created_at,
            message_metadata=display_projection.metadata,
            created_at=job.created_at,
            updated_at=job.updated_at,
            snapshot_version=self._queue.snapshot_version(job.session_id),
        )

    async def list(self, session_id: str) -> PendingRequestListDTO:
        async with self._lock:
            return self._snapshot_unlocked(session_id)

    async def list_summaries(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> PendingRequestSummaryListDTO:
        async with self._lock:
            jobs = self._get_jobs()
            job_ids = self._queue.ids(session_id)
            summaries = []
            for job_id in job_ids[:limit]:
                job = jobs.get(job_id)
                if job is None:
                    raise RuntimeError(
                        f"FIFO 队列引用不存在的 Job: session_id={session_id}, job_id={job_id}"
                    )
                entry = self._queue.entry(job_id)
                summaries.append(
                    PendingRequestSummaryDTO(
                        job_id=job.job_id,
                        message_id=job.message_id,
                        enqueue_sequence=entry.enqueue_sequence,
                        delivery_policy=entry.delivery_policy,
                        status="queued",
                        updated_at=job.updated_at,
                    )
                )
            return PendingRequestSummaryListDTO(
                session_id=session_id,
                active_job_id=self._get_current_jobs().get(session_id),
                requests=summaries,
                request_count=len(job_ids),
                snapshot_version=self._queue.snapshot_version(session_id),
                truncated=len(summaries) < len(job_ids),
            )

    def _snapshot_unlocked(self, session_id: str) -> PendingRequestListDTO:
        jobs = self._get_jobs()
        requests = []
        for position, job_id in enumerate(self._queue.ids(session_id)):
            job = jobs.get(job_id)
            if job is None:
                raise RuntimeError(
                    f"FIFO 队列引用不存在的 Job: session_id={session_id}, job_id={job_id}"
                )
            requests.append(self._dto(job, position))
        return PendingRequestListDTO(
            session_id=session_id,
            active_job_id=self._get_current_jobs().get(session_id),
            requests=requests,
            snapshot_version=self._queue.snapshot_version(session_id),
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
            self._queue.touch(session_id)
        return await self.list(session_id)

    async def update_policy(
        self,
        session_id: str,
        message_id: str,
        *,
        delivery_policy: DeliveryPolicy,
        expected_snapshot_version: int | None,
    ) -> PendingRequestListDTO:
        async with self._lock:
            current_version = self._queue.snapshot_version(session_id)
            if (
                expected_snapshot_version is not None
                and expected_snapshot_version != current_version
            ):
                raise RuntimeError(
                    f"队列快照已过期: session_id={session_id}, "
                    f"expected={expected_snapshot_version}, actual={current_version}"
                )
            job = self._job_by_message_id(session_id, message_id)
            self._queue.update_policy(session_id, job.job_id, delivery_policy)
            job.delivery_policy = delivery_policy
            job.updated_at = datetime.now()
        return await self.list(session_id)

    async def remove(self, session_id: str, message_id: str) -> PendingRequestListDTO:
        async with self._lock:
            job = self._job_by_message_id(session_id, message_id)
            self._queue.remove(session_id, job.job_id)
            transition_job_status(
                job,
                JobStatus.cancelled,
                error_message="消息已从队列撤回",
            )
        return await self.list(session_id)

    async def clear(self, session_id: str) -> PendingRequestListDTO:
        async with self._lock:
            removed = self._queue.clear(session_id)
            jobs = self._get_jobs()
            now = datetime.now()
            for entry in removed:
                job = jobs.get(entry.job_id)
                if job is not None:
                    transition_job_status(
                        job,
                        JobStatus.cancelled,
                        error_message="消息已从队列撤回",
                        now=now,
                    )
        return await self.list(session_id)

    async def reject_reorder(self, session_id: str) -> None:
        async with self._lock:
            self._queue.reject_reorder(session_id)
