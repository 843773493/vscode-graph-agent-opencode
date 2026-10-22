from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from app.abstractions.job_executor import JobInputOwnerProtocol
from app.abstractions.pending_request_store import PendingRequestStoreProtocol
from app.core.shielded_cleanup import run_sync_shielded
from app.domain.itemized.execution_admission import (
    ExecutionBinding,
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.schemas.internal_v2.common import JobStatus
from app.schemas.internal_v2.message import AttachmentRef
from app.schemas.internal_v2.pending_request import (
    DeliveryPolicy,
    PendingRequestControl,
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
    execution_binding: ExecutionBinding
    message_id: str
    session_id: str
    thread_id: str
    message: str
    agent_id: str
    gateway_id: str | None
    request_id: str | None
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
        get_current_jobs: Callable[[], dict[tuple[str, str], str]],
        persist_terminal_status: Callable[[PendingJob, str], Awaitable[bool]],
        input_owner: JobInputOwnerProtocol | None,
        store: PendingRequestStoreProtocol | None,
    ) -> None:
        self._queue = queue
        self._lock = lock
        self._get_jobs = get_jobs
        self._get_current_jobs = get_current_jobs
        self._persist_terminal_status = persist_terminal_status
        self._input_owner = input_owner
        self._store = store

    def _job_by_message_id(self, session_id: str, message_id: str) -> PendingJob:
        jobs = self._get_jobs()
        for job_id in self._queue.ids(session_id):
            job = jobs.get(job_id)
            if job is not None and job.message_id == message_id:
                return job
        raise ValueError(f"Session {session_id} 中不存在待处理消息 {message_id}")

    async def _dto(self, job: PendingJob, position: int, *, display: bool) -> PendingRequestDTO:
        entry = self._queue.entry(job.job_id)
        if job.delivery_policy is None:
            raise RuntimeError(f"待处理 Job 缺少 delivery_policy: job_id={job.job_id}")
        display_projection = project_message_for_display(
            job.message,
            job.message_metadata,
        )
        execution_binding = job.execution_binding
        if isinstance(execution_binding, UserExecutionIdentity):
            turn_id = execution_binding.turn_id
        elif isinstance(execution_binding, InternalExecutionIntent):
            turn_id = None
        else:
            raise TypeError("待处理 Job execution_binding 类型非法")
        result = PendingRequestDTO(
            job_id=job.job_id,
            execution_binding=execution_binding,
            message_id=job.message_id,
            session_id=job.session_id,
            thread_id=job.thread_id,
            turn_id=turn_id,
            content=display_projection.content if display else job.message,
            attachments=list(job.attachments),
            delivery_policy=entry.delivery_policy,
            enqueue_sequence=entry.enqueue_sequence,
            position=position,
            status="queued",
            waiting_reason=entry.waiting_reason,
            last_boundary=entry.last_boundary,
            agent_id=job.agent_id,
            gateway_id=job.gateway_id,
            request_id=job.request_id,
            message_created_at=job.message_created_at,
            message_metadata=display_projection.metadata if display else dict(job.message_metadata),
            created_at=job.created_at,
            updated_at=job.updated_at,
            snapshot_version=self._queue.snapshot_version(job.session_id),
        )
        if self._input_owner is None:
            return result
        reference = PendingRequestControl.model_validate(result.model_dump(exclude={
            "content", "attachments", "message_created_at", "message_metadata",
        }))
        message = await run_sync_shielded(self._input_owner.accepted_job_message, reference, display=display)
        return result.model_copy(update={
            "content": message.content, "attachments": message.attachments,
            "message_created_at": message.created_at.isoformat(),
            "message_metadata": {**message.metadata,
                                 "boxteam_session_provider_id": job.message_metadata.get("boxteam_session_provider_id")},
        })

    def _active_job_id(self, session_id: str, thread_id: str | None) -> str | None:
        current_jobs = self._get_current_jobs()
        if thread_id is not None:
            return current_jobs.get((session_id, thread_id))
        return next((value for key, value in current_jobs.items() if key[0] == session_id), None)

    async def list(
        self, session_id: str, *, display: bool = True, thread_id: str | None = None,
    ) -> PendingRequestListDTO:
        async with self._lock:
            return await self._snapshot_unlocked(session_id, display=display, thread_id=thread_id)

    async def list_summaries(
        self,
        session_id: str,
        *,
        limit: int,
        thread_id: str | None = None,
    ) -> PendingRequestSummaryListDTO:
        async with self._lock:
            jobs = self._get_jobs()
            job_ids = self._queue.ids(session_id, thread_id)
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
                        thread_id=job.thread_id,
                        enqueue_sequence=entry.enqueue_sequence,
                        delivery_policy=entry.delivery_policy,
                        status="queued",
                        updated_at=job.updated_at,
                    )
                )
            return PendingRequestSummaryListDTO(
                session_id=session_id,
                thread_id=thread_id,
                active_job_id=self._active_job_id(session_id, thread_id),
                requests=summaries,
                request_count=len(job_ids),
                snapshot_version=self._queue.snapshot_version(session_id),
                truncated=len(summaries) < len(job_ids),
            )

    async def _snapshot_unlocked(
        self, session_id: str, *, display: bool = True, thread_id: str | None = None,
    ) -> PendingRequestListDTO:
        jobs = self._get_jobs()
        requests = []
        for position, job_id in enumerate(self._queue.ids(session_id, thread_id)):
            job = jobs.get(job_id)
            if job is None:
                raise RuntimeError(
                    f"FIFO 队列引用不存在的 Job: session_id={session_id}, job_id={job_id}"
                )
            requests.append(await self._dto(job, position, display=display))
        return PendingRequestListDTO(
            session_id=session_id,
            thread_id=thread_id,
            active_job_id=self._active_job_id(session_id, thread_id),
            requests=requests,
            snapshot_version=self._queue.snapshot_version(session_id),
        )

    async def update_policy(
        self,
        session_id: str,
        message_id: str,
        *,
        delivery_policy: DeliveryPolicy,
        expected_snapshot_version: int | None,
    ) -> PendingRequestListDTO:
        async with self._lock:
            job = self._job_by_message_id(session_id, message_id)
            if self._store is None:
                raise RuntimeError("待处理消息控制缺少持久执行队列 owner")
            dto = await self._dto(job, 0, display=False)
            reference = PendingRequestControl.model_validate(dto.model_dump(exclude={
                "content", "attachments", "message_created_at", "message_metadata",
            }))
            await self._store.update_policy(reference, delivery_policy, expected_snapshot_version=expected_snapshot_version)
            self._queue.set_snapshot_version(session_id, await self._store.snapshot_version(session_id))
            self._queue.update_policy(session_id, job.job_id, delivery_policy)
            job.delivery_policy = delivery_policy
            job.updated_at = datetime.now()  # noqa: DTZ005
        return await self.list(session_id)

    async def remove(self, session_id: str, message_id: str) -> PendingRequestListDTO:
        async with self._lock:
            job = self._job_by_message_id(session_id, message_id)
            await self._persist_terminal_status(job, "cancelled")
            self._queue.remove(session_id, job.job_id)
            transition_job_status(
                job,
                JobStatus.cancelled,
                error_message="消息已从队列撤回",
            )
        return await self.list(session_id)

    async def clear(self, session_id: str) -> PendingRequestListDTO:
        async with self._lock:
            jobs = self._get_jobs()
            now = datetime.now()  # noqa: DTZ005
            for job_id in self._queue.ids(session_id):
                job = jobs[job_id]
                await self._persist_terminal_status(job, "cancelled")
                self._queue.remove(session_id, job_id)
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
