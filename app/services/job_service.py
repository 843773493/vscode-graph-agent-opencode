from __future__ import annotations
import asyncio
import uuid
from datetime import datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

from app.schemas.job import JobDTO, StepDTO, JobControlRequest, JobControlResponse
from app.schemas.common import JobStatus, RunMode, StepStatus, ControlAction
from app.services.agent_execution_service import AgentExecutionService
from app.core.event_bus import EventBus, EventType


@dataclass
class JobState:
    job_id: str
    session_id: str
    status: JobStatus
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
        self._bus = EventBus.get_instance()
    
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
                    entry_agent="deep_agent",
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
            entry_agent="deep_agent",
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

    async def control(self, job_id: str, control_request: JobControlRequest) -> JobControlResponse:
        job = self._jobs.get(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        
        if control_request.action == ControlAction.pause:
            job.status = JobStatus.paused
        elif control_request.action == ControlAction.resume:
            job.status = JobStatus.running
        elif control_request.action == ControlAction.cancel:
            job.status = JobStatus.cancelled
            if job.task and not job.task.done():
                job.task.cancel()
        
        job.updated_at = datetime.now()
        return JobControlResponse(
            job_id=job_id,
            status=job.status,
            control_state=f"Action {control_request.action.value} applied successfully"
        )
    
    async def run_agent(self, session_id: str, message: str) -> str:
        """
        启动Agent执行单步调用（同步阻塞模式，保持向后兼容）
        
        Args:
            session_id: 会话ID
            message: 用户输入消息
            
        Returns:
            Agent响应内容
        """
        agent_service = AgentExecutionService.get_instance()
        return await agent_service.run_step(session_id, message)
    
    async def start_job(self, session_id: str, message: str) -> str:
        """
        启动异步后台Job，不阻塞HTTP请求
        
        Args:
            session_id: 会话ID
            message: 用户输入消息
            
        Returns:
            job_id: 新创建的Job ID
        """
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        
        job = JobState(
            job_id=job_id,
            session_id=session_id,
            status=JobStatus.queued
        )
        
        self._jobs[job_id] = job
        
        # 启动后台异步任务
        job.task = asyncio.create_task(self._run_job_background(job_id, session_id, message))
        
        await self._bus.publish(
            job_id=job_id,
            event_type=EventType.JOB_CREATED,
            payload={"session_id": session_id, "message": message},
            agent_id="job_service"
        )
        
        return job_id
    
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
            result = await agent_service.run_step(session_id, message)
            
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
            job.status = JobStatus.cancelled
            job.ended_at = datetime.now()
            job.updated_at = datetime.now()
            await self._bus.publish(
                job_id=job_id,
                event_type=EventType.JOB_CANCELLED,
                payload={},
                agent_id="job_service"
            )
            
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
