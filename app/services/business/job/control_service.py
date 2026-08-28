from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, MutableMapping
from typing import Protocol

from app.schemas.internal_v2.common import (
    ControlAction,
    JobStatus,
)
from app.schemas.internal_v2.job import (
    JobControlRequest,
    JobControlResponseDTO,
)
from app.services.business.job.lifecycle import (
    ACTIVE_JOB_STATUSES,
    PAUSABLE_JOB_STATUSES,
    transition_job_status,
)
from app.services.business.job.pending_queue import JobPendingQueue
from app.services.business.job.pending_request_service import (
    JobPendingRequestService,
)


class JobControlTarget(Protocol):
    job_id: str
    session_id: str
    status: JobStatus
    task: asyncio.Task | None


class JobControlValueError(ValueError):
    @classmethod
    def unsupported_action(cls, action: ControlAction) -> JobControlValueError:
        return cls(f"Job 控制动作尚未实现: {action.value}")

    @classmethod
    def job_not_found(cls, job_id: str) -> JobControlValueError:
        return cls(f"Job {job_id} not found")

    @classmethod
    def pause_not_allowed(
        cls,
        job_id: str,
        status: JobStatus,
    ) -> JobControlValueError:
        return cls(
            "只有 running、streaming 或 waiting_input 的 Job 可以暂停: "
            f"job_id={job_id} status={status.value}"
        )

    @classmethod
    def resume_not_allowed(
        cls,
        job_id: str,
        status: JobStatus,
    ) -> JobControlValueError:
        return cls(
            "只有 paused Job 可以恢复: "
            f"job_id={job_id} status={status.value}"
        )

    @classmethod
    def terminal_job(cls, job_id: str, status: JobStatus) -> JobControlValueError:
        return cls(f"终态 Job 不允许取消: job_id={job_id} status={status.value}")

    @classmethod
    def session_busy(
        cls,
        session_id: str,
        current_job_id: str,
    ) -> JobControlValueError:
        return cls(
            "会话已有其他 active Job，不能恢复暂停任务: "
            f"session_id={session_id} current_job_id={current_job_id}"
        )


class JobControlRuntimeError(RuntimeError):
    @classmethod
    def no_task(cls, job_id: str, status: JobStatus) -> JobControlRuntimeError:
        return cls(
            "Job 没有可暂停的执行任务: "
            f"job_id={job_id} status={status.value}"
        )

    @classmethod
    def queue_mismatch(cls, job_id: str) -> JobControlRuntimeError:
        return cls(f"取消排队 Job 时队列状态不一致: job_id={job_id}")


class JobControlService:
    """负责 Job 用户控制动作及其与执行任务的协调。"""

    def __init__(
        self,
        *,
        get_jobs: Callable[[], Mapping[str, JobControlTarget]],
        get_current_jobs: Callable[[], MutableMapping[str, str]],
        pending_queue: JobPendingQueue,
        pending_requests: JobPendingRequestService,
        dispatch_lock: asyncio.Lock,
        start_job_task: Callable[[JobControlTarget], None],
    ) -> None:
        self._get_jobs = get_jobs
        self._get_current_jobs = get_current_jobs
        self._pending_queue = pending_queue
        self._pending_requests = pending_requests
        self._dispatch_lock = dispatch_lock
        self._start_job_task = start_job_task

    async def control(
        self,
        job_id: str,
        control_request: JobControlRequest,
    ) -> JobControlResponseDTO:
        if control_request.action not in {
            ControlAction.pause,
            ControlAction.resume,
            ControlAction.cancel,
        }:
            raise JobControlValueError.unsupported_action(control_request.action)

        task_to_cancel: asyncio.Task | None = None
        task_to_wait: asyncio.Task | None = None
        pending_session_id: str | None = None
        result: JobControlResponseDTO | None = None
        async with self._dispatch_lock:
            job = self._get_jobs().get(job_id)
            if job is None:
                raise JobControlValueError.job_not_found(job_id)

            if control_request.action == ControlAction.pause:
                if job.status not in PAUSABLE_JOB_STATUSES:
                    raise JobControlValueError.pause_not_allowed(
                        job_id,
                        job.status,
                    )
                if job.task is None or job.task.done():
                    raise JobControlRuntimeError.no_task(
                        job_id,
                        job.status,
                    )
                transition_job_status(job, JobStatus.paused)
                task_to_cancel = job.task
            elif control_request.action == ControlAction.resume:
                if job.status != JobStatus.paused:
                    raise JobControlValueError.resume_not_allowed(
                        job_id,
                        job.status,
                    )
                if job.task is not None and not job.task.done():
                    task_to_wait = job.task
                else:
                    self._resume_job_locked(job)
            elif job.status == JobStatus.queued:
                if not self._pending_queue.remove(job.session_id, job.job_id):
                    raise JobControlRuntimeError.queue_mismatch(job_id)
                transition_job_status(
                    job,
                    JobStatus.cancelled,
                    error_message="任务被用户取消",
                )
                pending_session_id = job.session_id
            elif job.status == JobStatus.paused:
                transition_job_status(
                    job,
                    JobStatus.cancelled,
                    error_message="任务被用户取消",
                )
            elif job.status == JobStatus.cancelling:
                if job.task is not None and not job.task.done():
                    task_to_cancel = job.task
                else:
                    transition_job_status(
                        job,
                        JobStatus.cancelled,
                        error_message="任务被用户取消",
                    )
            elif job.status in ACTIVE_JOB_STATUSES:
                if job.task is not None and not job.task.done():
                    transition_job_status(job, JobStatus.cancelling)
                    task_to_cancel = job.task
                else:
                    transition_job_status(
                        job,
                        JobStatus.cancelled,
                        error_message="任务被用户取消",
                    )
            else:
                raise JobControlValueError.terminal_job(
                    job_id,
                    job.status,
                )

            if task_to_wait is None:
                result = self._response(job_id, job, control_request)

        if task_to_cancel is not None:
            task_to_cancel.cancel()
        if task_to_wait is not None:
            task_to_wait.cancel()
            if isinstance(task_to_wait, asyncio.Future):
                await asyncio.gather(task_to_wait, return_exceptions=True)
            async with self._dispatch_lock:
                job = self._get_jobs().get(job_id)
                if job is None:
                    raise JobControlValueError.job_not_found(job_id)
                if job.status != JobStatus.paused:
                    raise JobControlValueError.resume_not_allowed(
                        job_id,
                        job.status,
                    )
                self._resume_job_locked(job)
                result = self._response(job_id, job, control_request)
        if pending_session_id is not None:
            await self._pending_requests.persist(
                await self._pending_requests.list(pending_session_id)
            )
        assert result is not None
        return result

    def _resume_job_locked(self, job: JobControlTarget) -> None:
        current_jobs = self._get_current_jobs()
        current_job_id = current_jobs.get(job.session_id)
        if current_job_id not in {None, job.job_id}:
            raise JobControlValueError.session_busy(
                job.session_id,
                current_job_id,
            )
        current_jobs[job.session_id] = job.job_id
        transition_job_status(job, JobStatus.running, error_message=None)
        if job.task is None or job.task.done():
            self._start_job_task(job)

    @staticmethod
    def _response(
        job_id: str,
        job: JobControlTarget,
        control_request: JobControlRequest,
    ) -> JobControlResponseDTO:
        return JobControlResponseDTO(
            job_id=job_id,
            status=job.status,
            control_state=(
                f"Action {control_request.action.value} applied successfully"
            ),
        )
