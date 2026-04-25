from __future__ import annotations
import asyncio
import uuid
from collections import deque
from datetime import datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

from app.schemas.job import JobDTO, StepDTO, JobControlRequest, JobControlResponseDTO
from app.schemas.common import JobStatus, RunMode, StepStatus, ControlAction
from app.services.agent_execution_service import AgentExecutionService
from app.core.job_event_bus import EventType, JobEventBus


@dataclass
class JobState:
    job_id: str
    session_id: str
    status: JobStatus
    message: str = ""
    agent_id: str = "deep_agent"
    progress: int = 0
    error_message: Optional[str] = None
    result: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    ended_at: Optional[datetime] = None
    task: Optional[asyncio.Task] = None
    steps: list[StepDTO] = field(default_factory=list)


class JobService:
    _instance: Optional["JobService"] = None
    _jobs: Dict[str, JobState] = {}
    
    def __init__(self):
        self._bus = JobEventBus.get_instance()
        self._session_current_job: dict[str, str] = {}
        self._session_waiting_jobs: dict[str, deque[str]] = {}
        self._dispatch_lock = asyncio.Lock()
    
    @classmethod
    def get_instance(cls) -> "JobService":
        if cls._instance is None:
            cls._instance = JobService()
        return cls._instance

    async def list(self, session_id: Optional[str] = None) -> list[JobDTO]:
        jobs = []
        for job in self._jobs.values():
            if session_id is None or job.session_id == session_id:
                jobs.append(JobDTO(
                    job_id=job.job_id,
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
    
    async def run_agent(self, session_id: str, message: str, agent_id: str = "deep_agent") -> str:
        """
        启动Agent执行单步调用（同步阻塞模式，保持向后兼容）
        
        Args:
            session_id: 会话ID
            message: 用户输入消息
            
        Returns:
            Agent响应内容
        """
        agent_service = AgentExecutionService.get_instance()
        # 同步接口也必须生成真实的job_id，永远不要传递None
        import uuid
        job_id = str(uuid.uuid4())
        return await agent_service.run_step(session_id, message, agent_id=agent_id, job_id=job_id)
    
    async def start_job(self, session_id: str, message: str, agent_id: str = "deep_agent") -> str:
        """
        启动异步后台Job，不阻塞HTTP请求
        
        Args:
            session_id: 会话ID
            message: 用户输入消息
            
        Returns:
            job_id: 新创建的Job ID
        """
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        
        job = JobState(
            job_id=job_id,
            session_id=session_id,
            message=message,
            agent_id=agent_id,
            status=JobStatus.queued
        )
        
        self._jobs[job_id] = job
        
        await self._bus.publish(
            job_id=job_id,
            event_type=EventType.JOB_CREATED,
                payload={"session_id": session_id, "message": message, "agent_id": agent_id},
            agent_id="job_service"
        )

        queued, blocked_by = await self._enqueue_or_dispatch(job)
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
                # 强制捕获任务异常，永远不要静默失败
                task.result()
            except Exception as e:
                import logging
                logging.error(f"Job task failed: job_id={job.job_id}, error={str(e)}", exc_info=True)
                self._job_failed(job.job_id, e)
        
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
        """后台执行Job的实际逻辑"""
        job = self._jobs[job_id]
        
        try:
            job.status = JobStatus.running
            job.updated_at = datetime.now()
            
            await self._bus.publish(
                job_id=job_id,
                event_type=EventType.JOB_STARTED,
                payload={},
                agent_id="job_service"
            )
            
            agent_service = AgentExecutionService.get_instance()
            result = await agent_service.run_step(session_id, message, agent_id=job.agent_id, job_id=job_id)

            from app.services.message_service import MessageService
            await MessageService.get_instance().append_assistant_message(
                session_id,
                result,
                metadata={
                    "source": "agent_execution",
                    "job_id": job_id,
                },
            )
            
            job.result = result
            job.status = JobStatus.completed
            job.progress = 100
            job.ended_at = datetime.now()
            job.updated_at = datetime.now()
            
            await self._bus.publish(
                job_id=job_id,
                event_type=EventType.JOB_COMPLETED,
                payload={"result": result},
                agent_id="job_service"
            )
            
        except asyncio.CancelledError:
            if job.status == JobStatus.paused:
                job.updated_at = datetime.now()
                await self._bus.publish(
                    job_id=job_id,
                    event_type=EventType.STATUS_CHANGE,
                    payload={"status": JobStatus.paused.value, "reason": "pause_requested"},
                    agent_id="job_service"
                )
            else:
                job.status = JobStatus.cancelled
                job.ended_at = datetime.now()
                await self._bus.publish(
                    job_id=job_id,
                    event_type=EventType.JOB_CANCELLED,
                    payload={},
                    agent_id="job_service"
                )

            job.updated_at = datetime.now()
            
        except Exception as e:
            job.status = JobStatus.failed
            job.error_message = str(e)
            job.ended_at = datetime.now()
            job.updated_at = datetime.now()
            await self._bus.publish(
                job_id=job_id,
                event_type=EventType.JOB_FAILED,
                payload={"error": str(e)},
                agent_id="job_service"
            )
        finally:
            await self._schedule_next_job_if_needed(job)

    def get_active_job_for_session(self, session_id: str) -> JobState | None:
        active_statuses = {
            JobStatus.accepted,
            JobStatus.queued,
            JobStatus.running,
            JobStatus.streaming,
            JobStatus.waiting_input,
            JobStatus.paused,
            JobStatus.interrupt_pending,
            JobStatus.cancelling,
        }

        candidates = [
            job
            for job in self._jobs.values()
            if job.session_id == session_id and job.status in active_statuses
        ]
        if not candidates:
            return None

        return max(candidates, key=lambda item: item.updated_at)
