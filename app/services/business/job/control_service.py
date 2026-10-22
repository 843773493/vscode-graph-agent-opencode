from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
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
from app.services.business.job.pending_request_controller import PendingJob
from app.services.business.job.pending_request_service import (
    JobPendingRequestService,
)


class JobControlTarget(PendingJob, Protocol):
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
        get_current_jobs: Callable[[], MutableMapping[tuple[str, str], str]],
        pending_queue: JobPendingQueue,
        pending_requests: JobPendingRequestService,
        dispatch_lock: asyncio.Lock,
        start_job_task: Callable[[JobControlTarget], None],
        schedule_after_cancel: Callable[[JobControlTarget], Awaitable[None]],
        persist_terminal_status: Callable[[PendingJob, str], Awaitable[bool]],
    ) -> None:
        self._get_jobs = get_jobs
        self._get_current_jobs = get_current_jobs
        self._pending_queue = pending_queue
        self._pending_requests = pending_requests
        self._dispatch_lock = dispatch_lock
        self._start_job_task = start_job_task
        self._persist_terminal_status = persist_terminal_status
        # 没有运行任务体的取消统一走权威终态收尾：
        # 终态写入与活动槽释放、FIFO 唤醒必须成对发生。
        self._schedule_after_cancel = schedule_after_cancel

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
        job_to_finalize: JobControlTarget | None = None
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
                await self._persist_terminal_status(job, "cancelled")
                if not self._pending_queue.remove(job.session_id, job.job_id):
                    raise JobControlRuntimeError.queue_mismatch(job_id)
                transition_job_status(
                    job,
                    JobStatus.cancelled,
                    error_message="任务被用户取消",
                )
                pending_session_id = job.session_id
            elif job.status in ACTIVE_JOB_STATUSES | {JobStatus.paused, JobStatus.cancelling}:
                was_paused = job.status == JobStatus.paused
                transition_job_status(job, JobStatus.cancelling)
                if job.task is not None and not job.task.done():
                    task_to_cancel = job.task
                    if was_paused:
                        job_to_finalize = job
                else:
                    # 暂停或启动失败后已无任务体负责 finally；统一写入权威
                    # 终态后再释放槽。落盘失败时保留 cancelling 以允许重试。
                    job_to_finalize = job
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
        if job_to_finalize is not None:
            if task_to_cancel is not None:
                # 暂停任务可能仍在退出；先让其任务体/回调结算，保持 paused
                # 取消返回终态的合同，同时避免两个收尾者并发发布事件。
                await asyncio.gather(task_to_cancel, return_exceptions=True)
                if not task_to_cancel.cancelled():
                    task_to_cancel.result()
            # 在锁外等待收尾：control 返回时活动槽必已释放、FIFO 队首已被唤醒，
            # 不会留下「已 terminal 但槽未释放」的可观察中间态。
            await self._schedule_after_cancel(job_to_finalize)
            result = self._response(job_id, job_to_finalize, control_request)
        if pending_session_id is not None:
            await self._pending_requests.refresh_version(pending_session_id)
        assert result is not None
        return result

    def _resume_job_locked(self, job: JobControlTarget) -> None:
        current_jobs = self._get_current_jobs()
        current_job_id = current_jobs.get((job.session_id, job.thread_id))
        if current_job_id not in {None, job.job_id}:
            raise JobControlValueError.session_busy(
                job.session_id,
                current_job_id,
            )
        current_jobs[(job.session_id, job.thread_id)] = job.job_id
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
            session_id=job.session_id,
            thread_id=job.thread_id,
            status=job.status,
            control_state=(
                f"Action {control_request.action.value} applied successfully"
            ),
        )
