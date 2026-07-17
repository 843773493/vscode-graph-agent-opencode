from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Dict

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_executor import JobExecutorProtocol
from app.core.identifier import create_prefixed_id
from app.core.job_event_bus import EventType
from app.schemas.public_v2.common import JobStatus, RunMode, ControlAction, MessageRole
from app.schemas.public_v2.job import JobDTO, StepDTO, JobControlRequest, JobControlResponseDTO
from app.schemas.public_v2.message import AttachmentRef
from app.services.business.job_runtime_state import JobRuntimeState


@dataclass
class JobState:
    job_id: str
    session_id: str
    message: str
    message_id: str
    message_created_at: str
    agent_id: str
    status: JobStatus
    message_role: MessageRole = MessageRole.user
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


class JobService:
    _jobs: Dict[str, JobState] = {}

    def __init__(self, *, job_event_bus: JobEventBusProtocol, job_executor: JobExecutorProtocol):
        self._bus: JobEventBusProtocol | None = job_event_bus
        self._session_current_job: dict[str, str] = {}
        self._session_waiting_jobs: dict[str, deque[str]] = {}
        self._dispatch_lock = asyncio.Lock()
        self._job_executor = job_executor

    def _normalize_result_text(self, result: object) -> str:
        if isinstance(result, str):
            return result
        return str(result)

    async def list(self, session_id: Optional[str] = None) -> list[JobDTO]:
        jobs = []
        for job in self._jobs.values():
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
                job.task = asyncio.create_task(self._run_job_background(job_id, job.session_id, job.message))
        elif control_request.action == ControlAction.cancel:
            job.status = JobStatus.cancelling
            if job.task and not job.task.done():
                job.task.cancel()

        job.updated_at = datetime.now()
        return JobControlResponseDTO(
            job_id=job_id,
            status=job.status,
            control_state=f"Action {control_request.action.value} applied successfully"
        )

    async def delete_session_jobs(self, session_id: str) -> int:
        async with self._dispatch_lock:
            jobs = [
                job
                for job in self._jobs.values()
                if job.session_id == session_id
            ]
            self._session_current_job.pop(session_id, None)
            self._session_waiting_jobs.pop(session_id, None)

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
            self._session_waiting_jobs.pop(session_id, None)

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
        message_role: MessageRole = MessageRole.user,
        message_metadata: dict[str, object] | None = None,
    ) -> str:
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
            message_role=message_role,
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

        queued, blocked_by = await self._enqueue_or_dispatch(job)
        logger.info("[job_service] enqueue_or_dispatch result: job_id=%s queued=%s blocked_by=%s", job_id, queued, blocked_by)
        if queued:
            await self._bus.publish(
                job_id=job_id,
                event_type=EventType.STATUS_CHANGE,
                payload={
                    "status": JobStatus.queued.value,
                    "reason": "waiting_previous_job",
                    "blocked_by_job_id": blocked_by,
                },
                agent_id="job_service",
            )

        return job_id

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

        job.task = loop.create_task(self._run_job_background(job.job_id, job.session_id, job.message))
        job.task.add_done_callback(_task_done_callback)

    async def _enqueue_or_dispatch(self, job: JobState) -> tuple[bool, str | None]:
        async with self._dispatch_lock:
            current_job_id = self._session_current_job.get(job.session_id)
            if current_job_id:
                current_job = self._jobs.get(current_job_id)
                if current_job and not self._is_terminal_status(current_job.status):
                    if job.session_id not in self._session_waiting_jobs:
                        self._session_waiting_jobs[job.session_id] = deque()
                    self._session_waiting_jobs[job.session_id].append(job.job_id)
                    job.status = JobStatus.queued
                    job.updated_at = datetime.now()
                    return True, current_job_id

            self._session_current_job[job.session_id] = job.job_id
            self._start_job_task(job)
            return False, None

    async def _schedule_next_job_if_needed(self, finished_job: JobState) -> None:
        if not self._is_terminal_status(finished_job.status):
            return

        next_job: JobState | None = None

        async with self._dispatch_lock:
            current_job_id = self._session_current_job.get(finished_job.session_id)
            if current_job_id != finished_job.job_id:
                return

            waiting = self._session_waiting_jobs.get(finished_job.session_id, deque())
            while waiting:
                next_job_id = waiting.popleft()
                candidate = self._jobs.get(next_job_id)
                if candidate and candidate.status == JobStatus.queued:
                    next_job = candidate
                    break

            if waiting:
                self._session_waiting_jobs[finished_job.session_id] = waiting
            else:
                self._session_waiting_jobs.pop(finished_job.session_id, None)

            if next_job is None:
                self._session_current_job.pop(finished_job.session_id, None)
                return

            self._session_current_job[finished_job.session_id] = next_job.job_id

        self._start_job_task(next_job)

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
                message_role=job.message_role,
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
                job.error_message = "任务被用户取消"
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
