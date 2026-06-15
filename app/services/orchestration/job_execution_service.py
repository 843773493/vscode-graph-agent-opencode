from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.core.job_event_bus import EventType
from app.schemas.public_v2.common import JobStatus

from app.services.business.message_service import MessageService
from app.abstractions.job_step_executor import JobStepExecutor


@dataclass
class JobRuntimeState:
    job_id: str
    session_id: str
    message: str
    agent_id: str
    message_id: str | None = None
    status: JobStatus = JobStatus.queued
    progress: int = 0
    error_message: Optional[str] = None
    result: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    ended_at: Optional[datetime] = None
    task: Optional[asyncio.Task] = None


class JobExecutionService:
    def __init__(
        self,
        *,
        agent_execution_service: JobStepExecutor,
        message_service: "MessageService",
        job_event_bus: JobEventBusProtocol,
    ) -> None:
        self._agent_execution_service = agent_execution_service
        self._message_service = message_service
        self._bus = job_event_bus

    async def run(self, job: JobRuntimeState) -> str:
        job.status = JobStatus.running
        job.updated_at = datetime.now()

        result = await self._agent_execution_service.run_step(
            job.session_id,
            job.message,
            agent_id=job.agent_id,
            job_id=job.job_id,
            message_id=job.message_id,
        )

        result_text = result if isinstance(result, str) else str(result)

        job.result = result_text
        job.status = JobStatus.completed
        job.progress = 100
        job.ended_at = datetime.now()
        job.updated_at = datetime.now()

        await self._bus.publish(
            job_id=job.job_id,
            event_type=EventType.JOB_COMPLETED,
            payload={"result": result_text},
            agent_id="job_service",
        )
        return result_text

    async def fail(self, job: JobRuntimeState, error: Exception) -> None:
        job.status = JobStatus.failed
        job.error_message = str(error)
        job.ended_at = datetime.now()
        job.updated_at = datetime.now()
        await self._bus.publish(
            job_id=job.job_id,
            event_type=EventType.JOB_FAILED,
            payload={"error": str(error)},
            agent_id="job_service",
        )
        
        