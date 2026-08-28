from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TypeVar

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_executor import JobExecutorProtocol
from app.abstractions.pending_request_store import PendingRequestStoreProtocol
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.schemas.internal_v2.common import JobStatus, RunMode
from app.schemas.internal_v2.job import (
    JobControlRequest,
    JobControlResponseDTO,
    JobDispatchSnapshotDTO,
    JobDTO,
    StepDTO,
)
from app.schemas.internal_v2.message import AttachmentRef
from app.schemas.internal_v2.pending_request import (
    DeliveryBoundary,
    DeliveryPolicy,
    PendingRequestListDTO,
    PendingRequestSummaryListDTO,
)
from app.services.business.job.control_service import JobControlService
from app.services.business.job.lifecycle import (
    TERMINAL_JOB_STATUSES,
    transition_job_status,
)
from app.services.business.job.pending_queue import (
    JobPendingQueue,
    QueueBoundary,
    QueueEntry,
)
from app.services.business.job.pending_request_service import (
    JobPendingRequestService,
)
from app.services.business.job.runtime_state import JobRuntimeState

T = TypeVar("T")
logger = logging.getLogger(__name__)


class JobAdmissionClosedError(RuntimeError):
    """Workspace API 正在排空时拒绝创建新的 Job。"""


@dataclass(frozen=True, slots=True)
class JobDrainBlocker:
    job_id: str
    session_id: str
    status: JobStatus
    phase: str | None
    tool_names: tuple[str, ...]


@dataclass
class JobState:
    job_id: str
    session_id: str
    message: str
    message_id: str
    message_created_at: str
    agent_id: str
    status: JobStatus
    message_metadata: dict[str, object] = field(default_factory=dict)
    attachments: list[AttachmentRef] = field(default_factory=list)
    progress: int = 0
    error_message: str | None = None
    result: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    ended_at: datetime | None = None
    task: asyncio.Task | None = None
    steps: list[StepDTO] = field(default_factory=list)
    delivery_policy: DeliveryPolicy | None = None
    delivery_boundary: DeliveryBoundary | None = None
    internal_reservation: bool = False
    cancellation_reason: str | None = None


class JobService:
    def __init__(
        self,
        *,
        job_event_bus: JobEventBusProtocol,
        job_executor: JobExecutorProtocol,
        pending_request_store: PendingRequestStoreProtocol | None = None,
    ):
        self._jobs: dict[str, JobState] = {}
        self._bus: JobEventBusProtocol | None = job_event_bus
        self._session_current_job: dict[str, str] = {}
        self._pending_queue = JobPendingQueue()
        self._dispatch_lock = asyncio.Lock()
        self._pending_restore_lock = asyncio.Lock()
        self._session_preparations: dict[str, int] = {}
        self._deleting_sessions: set[str] = set()
        self._accepting_jobs = True
        self._job_executor = job_executor
        self._pending_requests = JobPendingRequestService(
            queue=self._pending_queue,
            lock=self._dispatch_lock,
            store=pending_request_store,
            get_jobs=lambda: self._jobs,
            get_current_jobs=lambda: self._session_current_job,
        )
        self._control_service = JobControlService(
            get_jobs=lambda: self._jobs,
            get_current_jobs=lambda: self._session_current_job,
            pending_queue=self._pending_queue,
            pending_requests=self._pending_requests,
            dispatch_lock=self._dispatch_lock,
            start_job_task=lambda job: self._start_job_task(job),
        )

    def assert_accepting_jobs(self) -> None:
        if not self._accepting_jobs:
            raise JobAdmissionClosedError(
                "Workspace API 正在为安全重启排空任务，暂不接受新的 Job"
            )

    def close_admission(self) -> None:
        self._accepting_jobs = False

    def open_admission(self) -> None:
        self._accepting_jobs = True

    @property
    def accepting_jobs(self) -> bool:
        return self._accepting_jobs

    async def drain_blockers(self) -> list[JobDrainBlocker]:
        async with self._dispatch_lock:
            jobs = tuple(self._jobs.values())
        blockers: list[JobDrainBlocker] = []
        for job in jobs:
            if job.internal_reservation or self._is_terminal_status(job.status):
                continue
            interrupt_state = SessionInterruptState.get(job.session_id)
            blockers.append(
                JobDrainBlocker(
                    job_id=job.job_id,
                    session_id=job.session_id,
                    status=job.status,
                    phase=interrupt_state.phase,
                    tool_names=interrupt_state.active_tool_names,
                )
            )
        return blockers

    async def force_interrupt_active(self, *, reason: str) -> int:
        blockers = await self.drain_blockers()
        if not blockers:
            return 0

        active_blockers: list[JobDrainBlocker] = []
        sessions_with_queued_jobs: set[str] = set()
        queued_blocker_ids: set[str] = set()
        tasks: list[asyncio.Task] = []
        now = datetime.now()
        async with self._dispatch_lock:
            for blocker in blockers:
                job = self._jobs.get(blocker.job_id)
                if job is None or self._is_terminal_status(job.status):
                    continue
                active_blockers.append(blocker)
                job.cancellation_reason = reason
                if job.status in {JobStatus.accepted, JobStatus.queued}:
                    was_queued = job.status == JobStatus.queued
                    transition_job_status(
                        job,
                        JobStatus.cancelled,
                        error_message=reason,
                        now=now,
                    )
                    if was_queued:
                        sessions_with_queued_jobs.add(job.session_id)
                        queued_blocker_ids.add(job.job_id)
                    continue
                transition_job_status(job, JobStatus.cancelling, now=now)
                if job.task is not None and not job.task.done():
                    tasks.append(job.task)
            for session_id in sessions_with_queued_jobs:
                self._pending_queue.clear(session_id)

        if not active_blockers:
            return 0
        if self._bus is None:
            raise RuntimeError("JobService 未绑定 JobEventBus")
        for blocker in active_blockers:
            if blocker.job_id in queued_blocker_ids:
                continue
            await self._bus.publish(
                job_id=blocker.job_id,
                event_type=EventType.SESSION_INTERRUPTED,
                payload={
                    "session_id": blocker.session_id,
                    "phase": blocker.phase or "runtime_restart",
                    "tool_name": ", ".join(blocker.tool_names) or None,
                    "interrupted_at": datetime.now().astimezone().isoformat(),
                },
                agent_id="runtime_service",
            )

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        ended_at = datetime.now()
        for blocker in active_blockers:
            job = self._jobs.get(blocker.job_id)
            if job is None or job.status != JobStatus.cancelling:
                continue
            transition_job_status(
                job,
                JobStatus.cancelled,
                error_message=reason,
                now=ended_at,
            )
        for session_id in sessions_with_queued_jobs:
            await self._pending_requests.persist(
                await self._pending_requests.list(session_id)
            )
        return len(active_blockers)

    def _normalize_result_text(self, result: object) -> str:
        if isinstance(result, str):
            return result
        return str(result)

    async def list(self, session_id: str | None = None) -> list[JobDTO]:
        if session_id is not None:
            async def restore_and_list() -> list[JobDTO]:
                await self._ensure_pending_loaded(session_id)
                return self._list_loaded_jobs(session_id)

            return await self.run_session_preparation(
                session_id,
                restore_and_list,
            )
        return self._list_loaded_jobs(None)

    def _list_loaded_jobs(self, session_id: str | None) -> list[JobDTO]:
        jobs = []
        for job in self._jobs.values():
            if job.internal_reservation:
                continue
            if session_id is None or job.session_id == session_id:
                jobs.append(JobDTO(
                    job_id=job.job_id,
                    message_id=job.message_id,
                    session_id=job.session_id,
                    mode=RunMode.single_agent,
                    status=job.status,
                    entry_agent=job.agent_id,
                    progress=job.progress,
                    current_step=None,
                    error_message=job.error_message,
                    metadata={},
                    created_at=job.created_at,
                    updated_at=job.updated_at,
                    ended_at=job.ended_at
                ))
        return jobs

    async def get(self, job_id: str) -> JobDTO:
        job = self._jobs.get(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        return JobDTO(
            job_id=job.job_id,
            message_id=job.message_id,
            session_id=job.session_id,
            mode=RunMode.single_agent,
            status=job.status,
            entry_agent=job.agent_id,
            progress=job.progress,
            current_step=None,
            error_message=job.error_message,
            metadata={},
            created_at=job.created_at,
            updated_at=job.updated_at,
            ended_at=job.ended_at
        )

    async def run_session_idle_operation(
        self,
        session_id: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        """与 job admission 共用锁，仅在会话空闲期间执行 checkpoint 操作。"""
        async with self._dispatch_lock:
            if session_id in self._deleting_sessions:
                raise RuntimeError(f"会话正在删除，不能修改 checkpoint: {session_id}")
            preparation_count = self._session_preparations.get(session_id, 0)
            if preparation_count:
                raise RuntimeError(
                    "会话正在准备持久化消息，不能修改 checkpoint: "
                    f"session_id={session_id}, preparation_count={preparation_count}"
                )
            active_job_id = self._session_current_job.get(session_id)
            if active_job_id is not None:
                raise RuntimeError(
                    "会话存在运行中或正在收尾的任务，不能修改 checkpoint: "
                    f"session_id={session_id}, active_job_id={active_job_id}"
                )
            return await operation()

    async def run_sessions_idle_operation(
        self,
        session_ids: list[str],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        """与 job admission 共用锁，仅在所有目标会话空闲期间执行存储操作。"""
        normalized_session_ids = sorted(set(session_ids))
        async with self._dispatch_lock:
            for session_id in normalized_session_ids:
                if session_id in self._deleting_sessions:
                    raise RuntimeError(
                        f"会话正在删除，不能移动物理存储: {session_id}"
                    )
                preparation_count = self._session_preparations.get(session_id, 0)
                if preparation_count:
                    raise RuntimeError(
                        "会话正在准备持久化消息，不能移动物理存储: "
                        f"session_id={session_id}, "
                        f"preparation_count={preparation_count}"
                    )
                active_job_id = self._session_current_job.get(session_id)
                if active_job_id is not None:
                    raise RuntimeError(
                        "会话存在运行中或正在收尾的任务，不能移动物理存储: "
                        f"session_id={session_id}, active_job_id={active_job_id}"
                    )
            return await operation()

    async def run_session_preparation(
        self,
        session_id: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        """登记消息、附件和 Job 创建前的会话存储写入窗口。"""
        async with self._dispatch_lock:
            self.assert_accepting_jobs()
            if session_id in self._deleting_sessions:
                raise RuntimeError(f"会话正在删除，拒绝写入新消息: {session_id}")
            self._session_preparations[session_id] = (
                self._session_preparations.get(session_id, 0) + 1
            )
        try:
            return await operation()
        finally:
            async with self._dispatch_lock:
                remaining = self._session_preparations[session_id] - 1
                if remaining:
                    self._session_preparations[session_id] = remaining
                else:
                    self._session_preparations.pop(session_id, None)

    async def run_session_delete_operation(
        self,
        session_id: str,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        return await self.run_sessions_delete_operation([session_id], operation)

    async def run_sessions_delete_operation(
        self,
        session_ids: list[str],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        """关闭一组会话 admission，并在同一窗口内清理资源和物理目录。"""
        normalized_session_ids = sorted(set(session_ids))
        if not normalized_session_ids:
            return await operation()
        async with self._dispatch_lock:
            for session_id in normalized_session_ids:
                if session_id in self._deleting_sessions:
                    raise RuntimeError(f"会话删除已在进行: {session_id}")
                preparation_count = self._session_preparations.get(session_id, 0)
                if preparation_count:
                    raise RuntimeError(
                        "会话正在准备持久化消息，不能删除: "
                        f"session_id={session_id}, "
                        f"preparation_count={preparation_count}"
                    )
                active_job_id = self._session_current_job.get(session_id)
                if active_job_id is not None:
                    raise RuntimeError(
                        "会话存在运行中或正在收尾的任务，不能删除: "
                        f"session_id={session_id}, active_job_id={active_job_id}"
                    )
            self._deleting_sessions.update(normalized_session_ids)
        try:
            return await operation()
        except BaseException:
            async with self._dispatch_lock:
                self._deleting_sessions.difference_update(normalized_session_ids)
            raise

    async def list_steps(self, job_id: str) -> list[StepDTO]:
        job = self._jobs.get(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        return job.steps

    async def control(
        self,
        job_id: str,
        control_request: JobControlRequest,
    ) -> JobControlResponseDTO:
        job = self._jobs.get(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        return await self._control_service.control(job_id, control_request)

    async def list_pending(self, session_id: str) -> PendingRequestListDTO:
        async def restore_and_list() -> PendingRequestListDTO:
            await self._ensure_pending_loaded(session_id)
            return await self._pending_requests.list(session_id)

        return await self.run_session_preparation(session_id, restore_and_list)

    async def list_pending_summaries(
        self,
        session_id: str,
        *,
        limit: int,
    ) -> PendingRequestSummaryListDTO:
        async def list_prepared() -> PendingRequestSummaryListDTO:
            return await self._pending_requests.list_summaries(
                session_id,
                limit=limit,
            )

        return await self.run_session_preparation(session_id, list_prepared)

    async def update_pending(
        self,
        session_id: str,
        message_id: str,
        *,
        content: str,
        attachments: list[AttachmentRef],
    ) -> PendingRequestListDTO:
        async def update_prepared() -> PendingRequestListDTO:
            await self._ensure_pending_loaded(session_id)
            snapshot = await self._pending_requests.update(
                session_id,
                message_id,
                content=content,
                attachments=attachments,
            )
            await self._publish_pending(snapshot, "pending_request_updated")
            return snapshot

        return await self.run_session_preparation(session_id, update_prepared)

    async def update_pending_policy(
        self,
        session_id: str,
        message_id: str,
        *,
        delivery_policy: DeliveryPolicy,
        expected_snapshot_version: int | None = None,
    ) -> PendingRequestListDTO:
        async def update_prepared() -> PendingRequestListDTO:
            await self._ensure_pending_loaded(session_id)
            snapshot = await self._pending_requests.update_policy(
                session_id,
                message_id,
                delivery_policy=delivery_policy,
                expected_snapshot_version=expected_snapshot_version,
            )
            await self._publish_pending(snapshot, "pending_policy_updated")
            return snapshot

        return await self.run_session_preparation(session_id, update_prepared)

    async def remove_pending(
        self,
        session_id: str,
        message_id: str,
    ) -> PendingRequestListDTO:
        async def remove_prepared() -> PendingRequestListDTO:
            await self._ensure_pending_loaded(session_id)
            snapshot = await self._pending_requests.remove(session_id, message_id)
            await self._publish_pending(snapshot, "pending_request_removed")
            return snapshot

        return await self.run_session_preparation(session_id, remove_prepared)

    async def clear_pending(self, session_id: str) -> PendingRequestListDTO:
        async def clear_prepared() -> PendingRequestListDTO:
            await self._ensure_pending_loaded(session_id)
            snapshot = await self._pending_requests.clear(session_id)
            await self._publish_pending(snapshot, "pending_requests_cleared")
            return snapshot

        return await self.run_session_preparation(session_id, clear_prepared)

    async def reject_pending_reorder(self, session_id: str) -> None:
        async def reject_prepared() -> None:
            await self._ensure_pending_loaded(session_id)
            await self._pending_requests.reject_reorder(session_id)

        await self.run_session_preparation(session_id, reject_prepared)

    async def delete_session_jobs(self, session_id: str) -> int:
        async with self._dispatch_lock:
            jobs = [
                job
                for job in self._jobs.values()
                if job.session_id == session_id
            ]
            self._session_current_job.pop(session_id, None)
            self._pending_queue.clear(session_id)

            now = datetime.now()
            for job in jobs:
                if not self._is_terminal_status(job.status):
                    transition_job_status(
                        job,
                        JobStatus.cancelled,
                        error_message="会话删除时清理任务",
                        now=job.ended_at or now,
                    )

        for job in jobs:
            if job.task and not job.task.done():
                job.task.cancel()
                try:
                    await job.task
                except asyncio.CancelledError:
                    pass

        async with self._dispatch_lock:
            for job in jobs:
                self._jobs.pop(job.job_id, None)
            self._session_current_job.pop(session_id, None)
            self._pending_queue.clear(session_id)

        await self._pending_requests.delete(session_id)
        return len(jobs)

    async def start_job(
        self,
        session_id: str,
        message: str,
        *,
        job_id: str | None = None,
        agent_id: str = "default",
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        delivery_policy: DeliveryPolicy = "after_turn",
    ) -> JobDispatchSnapshotDTO:
        async def start_prepared() -> JobDispatchSnapshotDTO:
            return await self._start_job_prepared(
                session_id,
                message,
                job_id=job_id,
                agent_id=agent_id,
                message_id=message_id,
                attachments=attachments,
                message_created_at=message_created_at,
                message_metadata=message_metadata,
                delivery_policy=delivery_policy,
            )

        return await self.run_session_preparation(session_id, start_prepared)

    async def _start_job_prepared(
        self,
        session_id: str,
        message: str,
        *,
        job_id: str | None = None,
        agent_id: str = "default",
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        delivery_policy: DeliveryPolicy = "after_turn",
    ) -> JobDispatchSnapshotDTO:
        self.assert_accepting_jobs()
        async with self._dispatch_lock:
            if session_id in self._deleting_sessions:
                raise RuntimeError(f"会话正在删除，拒绝创建 Job: {session_id}")
        import logging
        logger = logging.getLogger(__name__)
        logger.info(
            "[job_service] start_job: session_id=%s agent_id=%s "
            "message_length=%s job_id=%s",
            session_id,
            agent_id,
            len(message or ""),
            "pending",
        )
        if not message_id:
            raise ValueError("创建 Job 时必须传入已持久化的用户 message_id")
        if not message_created_at:
            raise ValueError("创建 Job 时必须传入用户消息的 message_created_at")
        await self._ensure_pending_loaded(session_id)
        resolved_job_id = job_id or create_prefixed_id("job")
        logger.info(
            "[job_service] start_job assigned id: job_id=%s",
            resolved_job_id,
        )

        existing_job = self._jobs.get(resolved_job_id)
        if existing_job is not None:
            if (
                existing_job.session_id != session_id
                or existing_job.message_id != message_id
            ):
                raise RuntimeError(
                    "预留 Job ID 已被其它消息占用: "
                    f"job_id={resolved_job_id}, session_id={session_id}, "
                    f"message_id={message_id}"
                )
            return self._existing_dispatch_snapshot(existing_job)

        job = JobState(
            job_id=resolved_job_id,
            session_id=session_id,
            message=message,
            message_id=message_id,
            attachments=list(attachments or []),
            message_created_at=message_created_at,
            agent_id=agent_id,
            status=JobStatus.queued,
            message_metadata=dict(message_metadata or {}),
        )

        self._jobs[resolved_job_id] = job

        if self._bus is None:
            raise RuntimeError("JobService 未绑定 JobEventBus")

        dispatch = await self._enqueue_or_dispatch(
            job,
            delivery_policy=delivery_policy,
        )
        logger.info(
            "[job_service] enqueue_or_dispatch result: job_id=%s status=%s "
            "blocked_by=%s queued_ahead=%s pending=%s",
            resolved_job_id,
            dispatch.job_status,
            dispatch.blocked_by_job_id,
            dispatch.queued_jobs_ahead,
            dispatch.pending_job_count,
        )
        snapshot = await self._pending_requests.list(session_id)
        await self._pending_requests.persist(snapshot)

        await self._bus.publish(
            job_id=resolved_job_id,
            event_type=EventType.JOB_CREATED,
            payload={
                "session_id": session_id,
                "message": message,
                "agent_id": agent_id,
                "message_id": message_id,
                "message_created_at": message_created_at,
                "message_metadata": dict(message_metadata or {}),
                "attachments": [
                    attachment.model_dump(mode="json", exclude={"data_url"})
                    for attachment in job.attachments
                ],
                "delivery_policy": delivery_policy,
                "enqueue_sequence": dispatch.enqueue_sequence,
                "queue_snapshot_version": dispatch.queue_snapshot_version,
            },
            agent_id="job_service",
        )
        logger.info(
            "[job_service] JOB_CREATED published after queue commit: job_id=%s session_id=%s",
            resolved_job_id,
            session_id,
        )

        if dispatch.job_status == JobStatus.queued.value and dispatch.active_job_id is None:
            await self._start_next_pending(session_id, boundary="idle")
            dispatch = self._existing_dispatch_snapshot(job)
        await self._publish_pending(
            await self._pending_requests.list(session_id),
            "pending_request_enqueued",
        )
        return dispatch

    def _existing_dispatch_snapshot(
        self,
        job: JobState,
    ) -> JobDispatchSnapshotDTO:
        if self._is_terminal_status(job.status):
            raise RuntimeError(f"预留 Job 已结束，不能重复派发: {job.job_id}")
        queued_ids = self._pending_queue.ids(job.session_id)
        if job.job_id in queued_ids:
            queued_ahead = queued_ids.index(job.job_id)
            active_job_id = self._session_current_job.get(job.session_id)
            return JobDispatchSnapshotDTO(
                session_id=job.session_id,
                job_id=job.job_id,
                job_status="queued",
                active_job_id=active_job_id,
                blocked_by_job_id=active_job_id,
                queued_jobs_ahead=queued_ahead,
                queued_job_count=len(queued_ids),
                pending_job_count=len(queued_ids) + (1 if active_job_id else 0),
                delivery_policy=job.delivery_policy,
                enqueue_sequence=self._pending_queue.entry(job.job_id).enqueue_sequence,
                queue_snapshot_version=self._pending_queue.snapshot_version(job.session_id),
            )
        if self._session_current_job.get(job.session_id) != job.job_id:
            raise RuntimeError(
                "非终态 Job 既不在活动槽也不在持久队列: "
                f"job_id={job.job_id}, session_id={job.session_id}"
            )
        return JobDispatchSnapshotDTO(
            session_id=job.session_id,
            job_id=job.job_id,
            job_status="running",
            active_job_id=job.job_id,
            blocked_by_job_id=None,
            queued_jobs_ahead=0,
            queued_job_count=len(queued_ids),
            pending_job_count=1 + len(queued_ids),
            delivery_policy=None,
            queue_snapshot_version=self._pending_queue.snapshot_version(job.session_id),
        )

    def _is_terminal_status(self, status: JobStatus) -> bool:
        return status in TERMINAL_JOB_STATUSES

    def _start_job_task(self, job: JobState) -> None:
        loop = asyncio.get_running_loop()

        def _task_done_callback(task):
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.exception(
                    "Job task failed: job_id=%s",
                    job.job_id,
                )
                if not self._is_terminal_status(job.status):
                    transition_job_status(job, JobStatus.failed, error_message=str(e))
                if self._bus is not None:
                    asyncio.get_event_loop().create_task(
                        self._bus.publish(
                            job_id=job.job_id,
                            event_type=EventType.JOB_FAILED,
                            payload={"error": str(e)},
                            agent_id="job_service",
                        )
                    )

        # 每个 job 都是独立执行根，不能继承创建方工具调用中的 LangChain callback
        # 和 tracing ContextVar，否则跨会话 job 的模型事件会回流到发送方事件流。
        job.task = loop.create_task(
            self._run_job_background(job.job_id, job.session_id, job.message),
            context=contextvars.Context(),
        )
        job.task.add_done_callback(_task_done_callback)

    async def _enqueue_or_dispatch(
        self,
        job: JobState,
        *,
        delivery_policy: DeliveryPolicy,
    ) -> JobDispatchSnapshotDTO:
        async with self._dispatch_lock:
            entry = self._pending_queue.append(
                job.session_id,
                job.job_id,
                delivery_policy,
            )
            job.delivery_policy = delivery_policy
            transition_job_status(job, JobStatus.queued)
            current_job_id = self._session_current_job.get(job.session_id)
            current_job = self._jobs.get(current_job_id) if current_job_id else None
            if current_job is not None and self._is_terminal_status(current_job.status):
                self._session_current_job.pop(job.session_id, None)
                current_job_id = None
            waiting_ids = self._pending_queue.ids(job.session_id)
            queued_ids = tuple(
                queued_job_id
                for queued_job_id in waiting_ids
                if queued_job_id != current_job_id
            )
            queued_index = queued_ids.index(job.job_id)
            return JobDispatchSnapshotDTO(
                session_id=job.session_id,
                job_id=job.job_id,
                job_status="queued",
                active_job_id=current_job_id,
                blocked_by_job_id=current_job_id,
                queued_jobs_ahead=queued_index,
                queued_job_count=len(queued_ids),
                pending_job_count=len(queued_ids) + (1 if current_job_id else 0),
                delivery_policy=delivery_policy,
                enqueue_sequence=entry.enqueue_sequence,
                queue_snapshot_version=self._pending_queue.snapshot_version(job.session_id),
            )

    async def _schedule_next_job_if_needed(self, finished_job: JobState) -> None:
        should_continue = finished_job.status in {
            JobStatus.completed,
            JobStatus.succeeded,
            JobStatus.failed,
            JobStatus.timed_out,
        } or (
            finished_job.status == JobStatus.cancelled
            and finished_job.delivery_boundary == "after_interrupt"
        )
        if not should_continue:
            async with self._dispatch_lock:
                if finished_job.status == JobStatus.paused:
                    return
                if (
                    self._session_current_job.get(finished_job.session_id)
                    == finished_job.job_id
                ):
                    self._session_current_job.pop(finished_job.session_id, None)
            return

        async with self._dispatch_lock:
            current_job_id = self._session_current_job.get(finished_job.session_id)
            if current_job_id != finished_job.job_id:
                return

            if not self._pending_queue.peek_head(finished_job.session_id):
                self._session_current_job.pop(finished_job.session_id, None)
                return
        await self._start_next_pending(
            finished_job.session_id,
            boundary=finished_job.delivery_boundary or "after_turn",
            tool_result_available=False,
        )

    async def _run_job_background(self, job_id: str, session_id: str, message: str):
        job = self._jobs[job_id]
        logger.info(
            "[job_service] _run_job_background begin: "
            "job_id=%s session_id=%s agent_id=%s message_length=%s",
            job_id,
            session_id,
            job.agent_id,
            len(message or ""),
        )

        try:
            transition_job_status(job, JobStatus.running)

            if self._bus is not None:
                await self._bus.publish(
                    job_id=job_id,
                    event_type=EventType.JOB_STARTED,
                    payload={
                        "session_id": session_id,
                        "agent_id": job.agent_id,
                        "message": message,
                        "attachments": [
                            attachment.model_dump(mode="json")
                            for attachment in job.attachments
                        ],
                    },
                    agent_id="job_service",
                )

            result = await self._job_executor.run(JobRuntimeState(
                job_id=job.job_id,
                session_id=job.session_id,
                message=job.message,
                agent_id=job.agent_id,
                message_id=job.message_id,
                attachments=list(job.attachments),
                message_created_at=job.message_created_at,
                message_metadata=dict(job.message_metadata),
                status=job.status,
                progress=job.progress,
                error_message=job.error_message,
                result=job.result,
                created_at=job.created_at,
                updated_at=job.updated_at,
                ended_at=job.ended_at,
                task=job.task,
            ))
            job.result = result
            transition_job_status(job, JobStatus.completed)
            job.progress = 100
        except asyncio.CancelledError:
            if job.status == JobStatus.paused:
                transition_job_status(
                    job,
                    JobStatus.paused,
                    error_message="任务已暂停",
                )
            else:
                transition_job_status(
                    job,
                    JobStatus.cancelled,
                    error_message=job.cancellation_reason or "任务被用户取消",
                )
                if self._bus is not None:
                    await self._bus.publish(
                        job_id=job_id,
                        event_type=EventType.JOB_CANCELLED,
                        payload={},
                        agent_id="job_service",
                    )
        except Exception as error:  # noqa: BLE001
            transition_job_status(job, JobStatus.failed, error_message=str(error))
            if self._bus is not None:
                await self._bus.publish(
                    job_id=job_id,
                    event_type=EventType.JOB_FAILED,
                    payload={"error": str(error)},
                    agent_id="job_service",
                )
        finally:
            await self._schedule_next_job_if_needed(job)

    async def _ensure_pending_loaded(self, session_id: str) -> None:
        async with self._pending_restore_lock:
            async with self._dispatch_lock:
                if session_id in self._deleting_sessions:
                    raise RuntimeError(
                        f"会话正在删除，拒绝恢复待处理队列: {session_id}"
                    )
            should_resume = False
            records = await self._pending_requests.load_once(session_id)
            if not records:
                return
            async with self._dispatch_lock:
                if session_id in self._deleting_sessions:
                    raise RuntimeError(
                        f"会话正在删除，拒绝恢复待处理队列: {session_id}"
                    )
                restored: list[QueueEntry] = []
                for record in sorted(records, key=lambda item: item.enqueue_sequence):
                    self._jobs[record.job_id] = JobState(
                        job_id=record.job_id,
                        session_id=record.session_id,
                        message=record.content,
                        message_id=record.message_id,
                        message_created_at=record.message_created_at,
                        agent_id=record.agent_id,
                        status=JobStatus.queued,
                        message_metadata=dict(record.message_metadata),
                        attachments=list(record.attachments),
                        created_at=record.created_at,
                        updated_at=record.updated_at,
                        delivery_policy=record.delivery_policy,
                    )
                    restored.append(
                        QueueEntry(
                            job_id=record.job_id,
                            enqueue_sequence=record.enqueue_sequence,
                            delivery_policy=record.delivery_policy,
                            waiting_reason=record.waiting_reason,
                            last_boundary=record.last_boundary,
                            snapshot_version=record.snapshot_version,
                        )
                    )
                self._pending_queue.restore(session_id, restored)
                should_resume = bool(restored)
            if should_resume:
                await self._start_next_pending(session_id, boundary="idle")

    async def _publish_pending(
        self,
        snapshot: PendingRequestListDTO,
        reason: str,
    ) -> None:
        event_job_id = snapshot.active_job_id
        if event_job_id is None and snapshot.requests:
            event_job_id = snapshot.requests[0].job_id
        if event_job_id is None or self._bus is None:
            return
        head = snapshot.requests[0] if snapshot.requests else None
        boundary = (
            reason.removeprefix("boundary_")
            if reason.startswith("boundary_")
            else None
        )
        await self._bus.publish(
            job_id=event_job_id,
            event_type=EventType.STATUS_CHANGE,
            payload={
                "status": "running" if snapshot.active_job_id else "queued",
                "reason": reason,
                "session_id": snapshot.session_id,
                "message_id": head.message_id if head is not None else None,
                "enqueue_sequence": head.enqueue_sequence if head is not None else None,
                "delivery_policy": head.delivery_policy if head is not None else None,
                "boundary": boundary,
                "queue_snapshot_version": snapshot.snapshot_version,
                "requests": [
                    {
                        "message_id": request.message_id,
                        "job_id": request.job_id,
                        "enqueue_sequence": request.enqueue_sequence,
                        "delivery_policy": request.delivery_policy,
                        "status": request.status,
                        "waiting_reason": request.waiting_reason,
                    }
                    for request in snapshot.requests
                ],
            },
            agent_id="job_service",
        )

    async def _start_next_pending(
        self,
        session_id: str,
        *,
        boundary: QueueBoundary = "idle",
        tool_result_available: bool = True,
    ) -> bool:
        async with self._dispatch_lock:
            current_job_id = self._session_current_job.get(session_id)
            current_job = self._jobs.get(current_job_id) if current_job_id else None
            if current_job is not None and not self._is_terminal_status(current_job.status):
                return False
            entry = self._pending_queue.take_head(
                session_id,
                boundary,
                tool_result_available=tool_result_available,
            )
            if entry is None:
                self._session_current_job.pop(session_id, None)
                return False
            next_job = self._jobs.get(entry.job_id)
            if next_job is None:
                raise RuntimeError(
                    f"FIFO 队列引用不存在的 Job: session_id={session_id}, job_id={entry.job_id}"
                )
            self._session_current_job[session_id] = next_job.job_id
            next_job.delivery_boundary = entry.last_boundary
            transition_job_status(next_job, JobStatus.running)
        await self._pending_requests.persist(
            await self._pending_requests.list(session_id)
        )
        self._start_job_task(next_job)
        return True

    async def notify_boundary(
        self,
        session_id: str,
        boundary: DeliveryBoundary,
        *,
        tool_result_available: bool = True,
    ) -> PendingRequestListDTO:
        """记录边界，并在允许时从队列取出队首消息开始执行。"""
        await self._ensure_pending_loaded(session_id)
        async with self._dispatch_lock:
            current_job_id = self._session_current_job.get(session_id)
            current_job = self._jobs.get(current_job_id) if current_job_id else None
            if current_job is not None and not self._is_terminal_status(current_job.status):
                current_job.delivery_boundary = boundary
        if current_job is None or self._is_terminal_status(current_job.status):
            await self._start_next_pending(
                session_id,
                boundary=boundary,
                tool_result_available=tool_result_available,
            )
        snapshot = await self._pending_requests.list(session_id)
        await self._pending_requests.persist(snapshot)
        await self._publish_pending(snapshot, f"boundary_{boundary}")
        return snapshot
