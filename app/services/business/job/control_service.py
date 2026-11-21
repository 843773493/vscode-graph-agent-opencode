from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal, Protocol

from boxteam_plugin_sdk.lifecycle import run_cleanup_shielded

from app.core.execution_pause import ExecutionPause
from app.domain.itemized.execution_queue import ExecutionQueuePause, ExecutionQueueRecord
from app.schemas.internal_v2.common import ControlAction, JobStatus
from app.schemas.internal_v2.execution_control import ThreadExecutionControlDTO
from app.schemas.internal_v2.job import JobControlRequest, JobControlResponseDTO
from app.services.business.job.lifecycle import (
    ACTIVE_JOB_STATUSES, PAUSABLE_JOB_STATUSES, TERMINAL_JOB_STATUSES,
    transition_job_status,
)
from app.services.business.job.pending_queue import JobPendingQueue
from app.services.business.job.pending_request_controller import PendingJob
from app.services.business.job.pending_request_service import JobPendingRequestService


class JobControlTarget(PendingJob, Protocol):
    task: asyncio.Task | None
    execution_pause: ExecutionPause
    queue_record: ExecutionQueueRecord | None


class JobControlValueError(ValueError):
    pass


class JobControlRuntimeError(RuntimeError):
    pass


class JobControlService:
    """精确 Thread 暂停推进，Job 取消仍由原终态 owner 结算。"""

    def __init__(
        self, *, get_jobs: Callable[[], Mapping[str, JobControlTarget]],
        get_current_jobs: Callable[[], Mapping[tuple[str, str], str]],
        pending_queue: JobPendingQueue, pending_requests: JobPendingRequestService,
        dispatch_lock: asyncio.Lock,
        schedule_after_resume: Callable[[str, str], Awaitable[None]],
        schedule_after_cancel: Callable[[JobControlTarget], Awaitable[None]],
        persist_terminal_status: Callable[[PendingJob, str], Awaitable[bool]],
    ) -> None:
        self._get_jobs = get_jobs
        self._get_current_jobs = get_current_jobs
        self._pending_queue = pending_queue
        self._pending_requests = pending_requests
        self._dispatch_lock = dispatch_lock
        self._schedule_after_resume = schedule_after_resume
        self._schedule_after_cancel = schedule_after_cancel
        self._persist_terminal_status = persist_terminal_status
        # 只持有尚未恢复的 Thread 门；不复制队列、执行结果或任务生命周期。
        self._paused_threads: dict[tuple[str, str], ExecutionPause] = {}

    def advancement_paused(self, session_id: str, thread_id: str) -> bool:
        return (session_id, thread_id) in self._paused_threads

    def paused_threads(self, session_id: str, thread_id: str | None) -> set[str]:
        return {key[1] for key in self._paused_threads if key[0] == session_id and (thread_id is None or key[1] == thread_id)}

    def _current(self, session_id: str, thread_id: str) -> JobControlTarget | None:
        job_id = self._get_current_jobs().get((session_id, thread_id))
        job = self._get_jobs().get(job_id) if job_id else None
        return job if job is not None and job.status not in TERMINAL_JOB_STATUSES else None

    def restore_pauses(self, pauses: tuple[ExecutionQueuePause, ...]) -> None:
        for pause in pauses:
            gate = self._paused_threads.setdefault((pause.session_id, pause.thread_id), ExecutionPause())
            gate.request()

    async def _snapshot(self, session_id: str, thread_id: str) -> ThreadExecutionControlDTO:
        job = self._current(session_id, thread_id)
        gate = self._paused_threads.get((session_id, thread_id))
        pause = (await self._pending_requests.execution_queue.pause_state(session_id, thread_id)
                 if gate is not None and job is None else None)
        if job is not None and job.queue_record is None:
            raise JobControlRuntimeError(f"当前 Job 缺少权威 execution admission: {job.job_id}")
        return ThreadExecutionControlDTO(
            session_id=session_id, thread_id=thread_id,
            job_id=job.job_id if job else None,
            execution_id=(job.queue_record.cursor.admission.execution_id if job else
                          pause.execution_id if pause is not None and pause.outcome == "execution_lost" else None),
            state=gate.state if gate else "running" if job else "idle",
            advancement_paused=gate is not None,
            can_resume=gate is not None,
        )

    async def snapshot(self, session_id: str, thread_id: str) -> ThreadExecutionControlDTO:
        async with self._dispatch_lock:
            return await self._snapshot(session_id, thread_id)

    async def change_advancement(
        self, session_id: str, thread_id: str, *, action: Literal["pause", "resume"],
        expected_execution_id: str | None,
        resume_execution: Callable[[ExecutionQueuePause], Awaitable[str]] | None = None,
    ) -> ThreadExecutionControlDTO:
        async with self._dispatch_lock:
            observed = await self._snapshot(session_id, thread_id)
            if observed.execution_id != expected_execution_id:
                raise JobControlValueError("执行已变化，请刷新控制状态后重试")
            job = self._current(session_id, thread_id)
            if action == "pause":
                await self._change_gate(session_id, thread_id, action=action, job=job)
                return await self._snapshot(session_id, thread_id)
            pause = await self._pending_requests.execution_queue.pause_state(session_id, thread_id)
            if pause is None:
                raise JobControlValueError("当前 Thread 没有可继续的暂停")
        continuation_job_id = None
        if job is None and pause.outcome == "execution_lost":
            if resume_execution is None:
                raise JobControlRuntimeError("跨进程继续缺少内部执行准入 owner")
            # 不持调度锁进入消息准入；持久暂停门在新输入接受完成前保持关闭。
            continuation_job_id = await resume_execution(pause)
        async with self._dispatch_lock:
            await self._change_gate(session_id, thread_id, action="resume", job=job,
                                    pause_id=pause.pause_id, continuation_job_id=continuation_job_id)
        await self._schedule_after_resume(session_id, thread_id)
        return await self.snapshot(session_id, thread_id)

    async def _change_gate(
        self, session_id: str, thread_id: str, *, action: Literal["pause", "resume"],
        job: JobControlTarget | None, pause_id: str | None = None,
        continuation_job_id: str | None = None,
    ) -> None:
        async def commit() -> None:
            key = (session_id, thread_id)
            if action == "pause":
                if job is not None and job.status not in PAUSABLE_JOB_STATUSES:
                    raise JobControlValueError(f"当前 Job 状态不能暂停: {job.status.value}")
                if key in self._paused_threads:
                    return
                if job is not None and job.queue_record is None:
                    raise JobControlRuntimeError(f"当前 Job 缺少权威 execution admission: {job.job_id}")
                execution_id = job.queue_record.cursor.admission.execution_id if job else None
                await self._pending_requests.execution_queue.pause(session_id, thread_id, execution_id=execution_id)
                gate = job.execution_pause if job else ExecutionPause()
                self._paused_threads[key] = gate
                gate.request()
            else:
                pause = await self._pending_requests.execution_queue.pause_state(session_id, thread_id)
                if pause is None or pause_id is not None and pause.pause_id != pause_id:
                    raise JobControlValueError("Thread 暂停意图已变化，请刷新控制状态后重试")
                await self._pending_requests.execution_queue.resume(
                    session_id, thread_id, pause_id=pause.pause_id, continuation_job_id=continuation_job_id,
                )
                self._paused_threads.pop(key).resume()
        # 提交与内存发布必须一起结算；请求取消不能留下已落盘但仍推进的运行时。
        await run_cleanup_shielded(commit)

    async def clear_advancement(self, session_id: str, thread_id: str | None) -> None:
        """停止策略要求继续或丢弃队列时释放暂停门；调用方持有调度锁。"""
        for key in tuple(self._paused_threads):
            if key[0] == session_id and (thread_id is None or key[1] == thread_id):
                await self._change_gate(*key, action="resume", job=None)

    async def pause_pending_thread(self, session_id: str, thread_id: str) -> None:
        """停止已结算执行后仅保留队列暂停，取消旧的继续意图；调用方持调度锁。"""
        async def commit() -> None:
            await self._pending_requests.execution_queue.pause(session_id, thread_id, execution_id=None)
            self._paused_threads.setdefault((session_id, thread_id), ExecutionPause()).request()
        await run_cleanup_shielded(commit)

    async def control(self, job_id: str, control_request: JobControlRequest) -> JobControlResponseDTO:
        action = control_request.action
        if action not in {ControlAction.pause, ControlAction.resume, ControlAction.cancel}:
            raise JobControlValueError(f"Job 控制动作尚未实现: {action.value}")
        task_to_cancel: asyncio.Task | None = None
        job_to_finalize: JobControlTarget | None = None
        pending_session_id: str | None = None
        async with self._dispatch_lock:
            job = self._get_jobs().get(job_id)
            if job is None:
                raise JobControlValueError(f"Job {job_id} not found")
            if action in {ControlAction.pause, ControlAction.resume}:
                if job.status in TERMINAL_JOB_STATUSES:
                    raise JobControlValueError(f"终态 Job 不能暂停或继续: job_id={job_id} status={job.status.value}")
                if job.task is None or job.task.done():
                    raise JobControlRuntimeError(f"Job 没有可暂停的执行任务: job_id={job_id} status={job.status.value}")
                if self._current(job.session_id, job.thread_id) is not job:
                    raise JobControlValueError("Job 已不再是当前执行")
                await self._change_gate(job.session_id, job.thread_id,
                                  action="pause" if action == ControlAction.pause else "resume", job=job)
            elif job.status in {JobStatus.accepted, JobStatus.queued}:
                await self._persist_terminal_status(job, "cancelled")
                if job.status == JobStatus.queued:
                    self._pending_queue.remove(job.session_id, job.job_id)
                transition_job_status(job, JobStatus.cancelled, error_message="任务被用户取消")
                pending_session_id = job.session_id
            elif job.status in ACTIVE_JOB_STATUSES | {JobStatus.paused, JobStatus.cancelling}:
                transition_job_status(job, JobStatus.cancelling)
                if job.task is not None and not job.task.done():
                    task_to_cancel = job.task
                else:
                    job_to_finalize = job
            else:
                raise JobControlValueError(f"终态 Job 不允许取消: job_id={job_id} status={job.status.value}")
        if task_to_cancel is not None:
            task_to_cancel.cancel()
        if job_to_finalize is not None:
            await self._schedule_after_cancel(job_to_finalize)
        if pending_session_id is not None:
            await self._pending_requests.refresh_version(pending_session_id)
        if action == ControlAction.resume:
            await self._schedule_after_resume(job.session_id, job.thread_id)
        return JobControlResponseDTO(
            job_id=job_id, session_id=job.session_id, thread_id=job.thread_id,
            status=job.status, control_state=job.execution_pause.state,
        )
