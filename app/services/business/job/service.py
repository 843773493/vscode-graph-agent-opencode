from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, Optional, TypeVar

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_executor import JobExecutorProtocol
from app.abstractions.pending_request_store import PendingRequestStoreProtocol
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.core.session_interrupt_state import SessionInterruptState
from app.schemas.public_v2.common import JobStatus, RunMode, ControlAction
from app.schemas.public_v2.job import (
    JobControlRequest,
    JobControlResponseDTO,
    JobDispatchSnapshotDTO,
    JobDTO,
    StepDTO,
)
from app.schemas.public_v2.message import AttachmentRef
from app.schemas.public_v2.pending_request import (
    PendingRequestKind,
    PendingRequestListDTO,
    PendingRequestOrderItem,
)
from app.services.business.job.pending_queue import JobPendingQueue
from app.services.business.job.pending_request_service import (
    JobPendingRequestService,
)
from app.services.business.job.runtime_state import JobRuntimeState

T = TypeVar("T")


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
    error_message: Optional[str] = None
    result: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    ended_at: Optional[datetime] = None
    task: Optional[asyncio.Task] = None
    steps: list[StepDTO] = field(default_factory=list)
    pending_kind: PendingRequestKind | None = None
    dispatch_pending_after_cancel: bool = False
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
        self._jobs: Dict[str, JobState] = {}
        self._bus: JobEventBusProtocol | None = job_event_bus
        self._session_current_job: dict[str, str] = {}
        self._pending_queue = JobPendingQueue()
        self._dispatch_lock = asyncio.Lock()
        self._pending_restore_lock = asyncio.Lock()
        self._accepting_jobs = True
        self._job_executor = job_executor
        self._pending_requests = JobPendingRequestService(
            queue=self._pending_queue,
            lock=self._dispatch_lock,
            store=pending_request_store,
            get_jobs=lambda: self._jobs,
            get_current_jobs=lambda: self._session_current_job,
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

        blocker_ids = {blocker.job_id for blocker in blockers}
        sessions_with_queued_jobs: set[str] = set()
        tasks: list[asyncio.Task] = []
        now = datetime.now()
        async with self._dispatch_lock:
            for job_id in blocker_ids:
                job = self._jobs[job_id]
                job.cancellation_reason = reason
                job.updated_at = now
                if job.status == JobStatus.queued:
                    job.status = JobStatus.cancelled
                    job.error_message = reason
                    job.ended_at = now
                    sessions_with_queued_jobs.add(job.session_id)
                    continue
                job.status = JobStatus.cancelling
                if job.task is not None and not job.task.done():
                    tasks.append(job.task)
            for session_id in sessions_with_queued_jobs:
                self._pending_queue.clear(session_id)

        if self._bus is None:
            raise RuntimeError("JobService 未绑定 JobEventBus")
        for blocker in blockers:
            if blocker.status == JobStatus.queued:
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
        for blocker in blockers:
            job = self._jobs[blocker.job_id]
            if job.status != JobStatus.cancelling:
                continue
            job.status = JobStatus.cancelled
            job.error_message = reason
            job.ended_at = ended_at
            job.updated_at = ended_at
        for session_id in sessions_with_queued_jobs:
            await self._pending_requests.persist(
                await self._pending_requests.list(session_id)
            )
        return len(blockers)

    def _normalize_result_text(self, result: object) -> str:
        if isinstance(result, str):
            return result
        return str(result)

    async def list(self, session_id: Optional[str] = None) -> list[JobDTO]:
        if session_id is not None:
            await self._ensure_pending_loaded(session_id)
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
            active_job_id = self._session_current_job.get(session_id)
            if active_job_id is not None:
                raise RuntimeError(
                    "会话存在运行中或正在收尾的任务，不能修改 checkpoint: "
                    f"session_id={session_id}, active_job_id={active_job_id}"
                )
            return await operation()

    async def list_steps(self, job_id: str) -> list[StepDTO]:
        job = self._jobs.get(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        return job.steps

    async def control(self, job_id: str, control_request: JobControlRequest) -> JobControlResponseDTO:
        job = self._jobs.get(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        if control_request.action == ControlAction.pause:
            job.status = JobStatus.paused
            if job.task and not job.task.done():
                job.task.cancel()
        elif control_request.action == ControlAction.resume:
            job.status = JobStatus.running
            if job.task is None or job.task.done():
                self._start_job_task(job)
        elif control_request.action == ControlAction.cancel:
            if job.status == JobStatus.queued:
                await self._pending_requests.remove(
                    job.session_id,
                    job.message_id,
                )
            else:
                job.status = JobStatus.cancelling
            if job.task and not job.task.done():
                job.task.cancel()

        job.updated_at = datetime.now()
        return JobControlResponseDTO(
            job_id=job_id,
            status=job.status,
            control_state=f"Action {control_request.action.value} applied successfully"
        )

    async def list_pending(self, session_id: str) -> PendingRequestListDTO:
        await self._ensure_pending_loaded(session_id)
        return await self._pending_requests.list(session_id)

    async def update_pending(
        self,
        session_id: str,
        message_id: str,
        *,
        content: str,
        attachments: list[AttachmentRef],
    ) -> PendingRequestListDTO:
        await self._ensure_pending_loaded(session_id)
        snapshot = await self._pending_requests.update(
            session_id,
            message_id,
            content=content,
            attachments=attachments,
        )
        await self._publish_pending(snapshot, "pending_request_updated")
        return snapshot

    async def remove_pending(
        self,
        session_id: str,
        message_id: str,
    ) -> PendingRequestListDTO:
        await self._ensure_pending_loaded(session_id)
        snapshot = await self._pending_requests.remove(session_id, message_id)
        await self._publish_pending(snapshot, "pending_request_removed")
        return snapshot

    async def clear_pending(self, session_id: str) -> PendingRequestListDTO:
        await self._ensure_pending_loaded(session_id)
        snapshot = await self._pending_requests.clear(session_id)
        await self._publish_pending(snapshot, "pending_requests_cleared")
        return snapshot

    async def reorder_pending(
        self,
        session_id: str,
        requests: list[PendingRequestOrderItem],
    ) -> PendingRequestListDTO:
        await self._ensure_pending_loaded(session_id)
        snapshot = await self._pending_requests.reorder(session_id, requests)
        await self._publish_pending(snapshot, "pending_requests_reordered")
        return snapshot

    async def send_pending_immediately(
        self,
        session_id: str,
        message_id: str,
    ) -> PendingRequestListDTO:
        await self._ensure_pending_loaded(session_id)
        reservation = self._new_dispatch_reservation(session_id)
        try:
            snapshot, reserved = (
                await self._pending_requests.promote_and_reserve_if_idle(
                    session_id,
                    message_id,
                    reservation,
                )
            )
        except BaseException:
            try:
                await self._start_next_pending(session_id)
            except BaseException:
                import logging

                logging.getLogger(__name__).exception(
                    "立即发送持久化失败后的队列恢复也失败: session_id=%s",
                    session_id,
                )
            raise
        if reserved:
            reservation.status = JobStatus.completed
            try:
                await self._schedule_next_job_if_needed(reservation)
            finally:
                self._jobs.pop(reservation.job_id, None)
        else:
            current_job_id = snapshot.active_job_id
            current_job = (
                self._jobs.get(current_job_id) if current_job_id else None
            )
            if (
                current_job is not None
                and current_job.task is not None
                and not current_job.task.done()
            ):
                current_job.status = JobStatus.cancelling
                current_job.error_message = "为立即发送排队消息而停止"
                current_job.dispatch_pending_after_cancel = True
                current_job.updated_at = datetime.now()
                SessionInterruptState.set(
                    session_id,
                    cancellation_reason="pending_request_send_immediately",
                )
                current_job.task.cancel()
        snapshot = await self._pending_requests.list(session_id)
        await self._publish_pending(
            snapshot,
            "pending_request_send_immediately",
        )
        return snapshot

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
                    job.status = JobStatus.cancelled
                    job.error_message = "会话删除时清理任务"
                    job.ended_at = job.ended_at or now
                    job.updated_at = now

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
        agent_id: str = "default",
        message_id: str,
        attachments: list[AttachmentRef] | None = None,
        message_created_at: str,
        message_metadata: dict[str, object] | None = None,
        pending_kind: PendingRequestKind = "queued",
    ) -> JobDispatchSnapshotDTO:
        self.assert_accepting_jobs()
        import logging
        logger = logging.getLogger(__name__)
        logger.info(
            "[job_service] start_job: session_id=%s agent_id=%s message_length=%s job_id=%s",
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
        job_id = create_prefixed_id("job")
        logger.info("[job_service] start_job assigned id: job_id=%s", job_id)

        job = JobState(
            job_id=job_id,
            session_id=session_id,
            message=message,
            message_id=message_id,
            attachments=list(attachments or []),
            message_created_at=message_created_at,
            agent_id=agent_id,
            status=JobStatus.queued,
            message_metadata=dict(message_metadata or {}),
        )

        self._jobs[job_id] = job

        if self._bus is None:
            raise RuntimeError("JobService 未绑定 JobEventBus")

        await self._bus.publish(
            job_id=job_id,
            event_type=EventType.JOB_CREATED,
            payload={
                "session_id": session_id,
                "message": message,
                "agent_id": agent_id,
                "attachments": [
                    attachment.model_dump(mode="json", exclude={"data_url"})
                    for attachment in job.attachments
                ],
            },
            agent_id="job_service"
        )
        logger.info("[job_service] JOB_CREATED published: job_id=%s session_id=%s", job_id, session_id)

        dispatch = await self._enqueue_or_dispatch(job, pending_kind=pending_kind)
        logger.info(
            "[job_service] enqueue_or_dispatch result: job_id=%s status=%s blocked_by=%s queued_ahead=%s pending=%s",
            job_id,
            dispatch.job_status,
            dispatch.blocked_by_job_id,
            dispatch.queued_jobs_ahead,
            dispatch.pending_job_count,
        )
        if dispatch.job_status == JobStatus.queued.value:
            snapshot = await self._pending_requests.list(session_id)
            await self._pending_requests.persist(snapshot)
            await self._bus.publish(
                job_id=job_id,
                event_type=EventType.STATUS_CHANGE,
                payload={
                    "status": JobStatus.queued.value,
                    "reason": "pending_request_enqueued",
                    "session_id": session_id,
                    "blocked_by_job_id": dispatch.blocked_by_job_id,
                    "queued_jobs_ahead": dispatch.queued_jobs_ahead,
                    "queued_job_count": dispatch.queued_job_count,
                    "pending_job_count": dispatch.pending_job_count,
                },
                agent_id="job_service",
            )

        return dispatch

    def _is_terminal_status(self, status: JobStatus) -> bool:
        return status in {
            JobStatus.completed,
            JobStatus.succeeded,
            JobStatus.failed,
            JobStatus.cancelled,
            JobStatus.timed_out,
        }

    def _start_job_task(self, job: JobState) -> None:
        loop = asyncio.get_running_loop()

        def _task_done_callback(task):
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                import logging
                logging.error(f"Job task failed: job_id={job.job_id}, error={str(e)}", exc_info=True)
                job.status = JobStatus.failed
                job.error_message = str(e)
                job.ended_at = datetime.now()
                job.updated_at = datetime.now()
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
        pending_kind: PendingRequestKind,
    ) -> JobDispatchSnapshotDTO:
        async with self._dispatch_lock:
            current_job_id = self._session_current_job.get(job.session_id)
            if current_job_id:
                current_job = self._jobs.get(current_job_id)
                if current_job and not self._is_terminal_status(current_job.status):
                    queued_jobs_ahead = self._pending_queue.append(
                        job.session_id,
                        job.job_id,
                        pending_kind,
                    )
                    job.status = JobStatus.queued
                    job.pending_kind = pending_kind
                    job.updated_at = datetime.now()
                    waiting_job_count = len(self._pending_queue.ids(job.session_id))
                    return JobDispatchSnapshotDTO(
                        session_id=job.session_id,
                        job_id=job.job_id,
                        job_status="queued",
                        active_job_id=current_job_id,
                        blocked_by_job_id=current_job_id,
                        queued_jobs_ahead=queued_jobs_ahead,
                        queued_job_count=waiting_job_count,
                        pending_job_count=1 + waiting_job_count,
                        pending_kind=pending_kind,
                    )

            self._session_current_job[job.session_id] = job.job_id
            job.status = JobStatus.running
            job.updated_at = datetime.now()
            self._start_job_task(job)
            return JobDispatchSnapshotDTO(
                session_id=job.session_id,
                job_id=job.job_id,
                job_status="running",
                active_job_id=job.job_id,
                blocked_by_job_id=None,
                queued_jobs_ahead=0,
                queued_job_count=0,
                pending_job_count=1,
                pending_kind=None,
            )

    async def _schedule_next_job_if_needed(self, finished_job: JobState) -> None:
        should_continue = finished_job.status in {
            JobStatus.completed,
            JobStatus.succeeded,
        } or (
            finished_job.status == JobStatus.cancelled
            and finished_job.dispatch_pending_after_cancel
        )
        if not should_continue:
            async with self._dispatch_lock:
                if (
                    self._session_current_job.get(finished_job.session_id)
                    == finished_job.job_id
                ):
                    self._session_current_job.pop(finished_job.session_id, None)
            return

        next_job: JobState | None = None

        async with self._dispatch_lock:
            current_job_id = self._session_current_job.get(finished_job.session_id)
            if current_job_id != finished_job.job_id:
                return

            while True:
                next_group = self._pending_queue.pop_next_group(
                    finished_job.session_id
                )
                if not next_group:
                    break
                next_job_id = next_group[0]
                candidate = self._jobs.get(next_job_id)
                if candidate and candidate.status == JobStatus.queued:
                    next_job = candidate
                    if len(next_group) > 1:
                        merged_jobs = [
                            self._jobs[group_job_id]
                            for group_job_id in next_group
                            if group_job_id in self._jobs
                        ]
                        next_job.message = "\n\n".join(
                            merged_job.message for merged_job in merged_jobs
                        )
                        next_job.attachments = [
                            attachment
                            for merged_job in merged_jobs
                            for attachment in merged_job.attachments
                        ]
                        now = datetime.now()
                        for merged_job in merged_jobs[1:]:
                            merged_job.status = JobStatus.cancelled
                            merged_job.error_message = (
                                f"Steering 消息已合并到 {next_job.job_id}"
                            )
                            merged_job.ended_at = now
                            merged_job.updated_at = now
                    break

            if next_job is None:
                self._session_current_job.pop(finished_job.session_id, None)
                return

            self._session_current_job[finished_job.session_id] = next_job.job_id
            next_job.pending_kind = None

        self._start_job_task(next_job)
        await self._pending_requests.persist(
            await self._pending_requests.list(finished_job.session_id)
        )

    async def _run_job_background(self, job_id: str, session_id: str, message: str):
        job = self._jobs[job_id]
        import logging
        logger = logging.getLogger(__name__)
        logger.info("[job_service] _run_job_background begin: job_id=%s session_id=%s agent_id=%s message_length=%s", job_id, session_id, job.agent_id, len(message or ""))

        try:
            job.status = JobStatus.running
            job.updated_at = datetime.now()

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
                yield_requested=lambda: self._pending_queue.yield_requested(
                    job.session_id
                ),
            ))
            job.result = result
            job.status = JobStatus.completed
            job.progress = 100
            job.ended_at = datetime.now()
            job.updated_at = datetime.now()
        except asyncio.CancelledError:
            if job.status == JobStatus.paused:
                job.error_message = "任务已暂停"
                job.updated_at = datetime.now()
            else:
                job.status = JobStatus.cancelled
                job.error_message = job.cancellation_reason or "任务被用户取消"
                job.ended_at = datetime.now()
                job.updated_at = datetime.now()
                if self._bus is not None:
                    await self._bus.publish(
                        job_id=job_id,
                        event_type=EventType.JOB_CANCELLED,
                        payload={},
                        agent_id="job_service",
                    )
        except Exception as error:
            job.status = JobStatus.failed
            job.error_message = str(error)
            job.ended_at = datetime.now()
            job.updated_at = datetime.now()
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
            should_resume = False
            records = await self._pending_requests.load_once(session_id)
            if not records:
                return
            async with self._dispatch_lock:
                restored: list[tuple[str, PendingRequestKind]] = []
                for record in sorted(records, key=lambda item: item.position):
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
                        pending_kind=record.kind,
                    )
                    restored.append((record.job_id, record.kind))
                self._pending_queue.restore(session_id, restored)
                should_resume = bool(restored)
            if should_resume:
                await self._start_next_pending(session_id)

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
        await self._bus.publish(
            job_id=event_job_id,
            event_type=EventType.STATUS_CHANGE,
            payload={
                "status": "running" if snapshot.active_job_id else "queued",
                "reason": reason,
                "session_id": snapshot.session_id,
                "yield_requested": snapshot.yield_requested,
            },
            agent_id="job_service",
        )

    async def _start_next_pending(self, session_id: str) -> None:
        placeholder = self._new_dispatch_reservation(session_id)
        async with self._dispatch_lock:
            current_job_id = self._session_current_job.get(session_id)
            current_job = self._jobs.get(current_job_id) if current_job_id else None
            if current_job is not None and not self._is_terminal_status(
                current_job.status
            ):
                return
            self._session_current_job[session_id] = placeholder.job_id
            self._jobs[placeholder.job_id] = placeholder
        placeholder.status = JobStatus.completed
        try:
            await self._schedule_next_job_if_needed(placeholder)
        finally:
            self._jobs.pop(placeholder.job_id, None)

    @staticmethod
    def _new_dispatch_reservation(session_id: str) -> JobState:
        return JobState(
            job_id=create_prefixed_id("completed"),
            session_id=session_id,
            message="",
            message_id=create_prefixed_id("msg"),
            message_created_at=datetime.now().isoformat(),
            agent_id="default",
            status=JobStatus.running,
            internal_reservation=True,
        )
