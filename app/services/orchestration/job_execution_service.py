from __future__ import annotations

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.abstractions.job_executor import JobRuntimeStateProtocol
from app.abstractions.job_step_executor import JobStepExecutor
from app.core.job_event_bus import EventType
from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.lifecycle import transition_job_status
from app.services.business.message_service import MessageService
from app.services.orchestration.session_title_service import SessionTitleService


def session_title_message(
    message: str,
    message_metadata: dict[str, object],
) -> str | None:
    """选择可用于会话标题的用户可见文本。"""
    if message_metadata.get("internal") is not True:
        return message
    if message_metadata.get("goal_continuation") is not True:
        return None
    objective = message_metadata.get("goal_objective")
    return (
        objective.strip()
        if isinstance(objective, str) and objective.strip()
        else None
    )


class JobExecutionService:
    def __init__(
        self,
        *,
        agent_execution_service: JobStepExecutor,
        message_service: MessageService,
        job_event_bus: JobEventBusProtocol,
        session_title_service: SessionTitleService,
    ) -> None:
        self._agent_execution_service = agent_execution_service
        self._message_service = message_service
        self._bus = job_event_bus
        self._session_title_service = session_title_service

    async def run(self, job: JobRuntimeStateProtocol) -> str:
        transition_job_status(job, JobStatus.running)

        try:
            title_message = session_title_message(job.message, job.message_metadata)
            if title_message is not None:
                await self._session_title_service.maybe_auto_title_before_first_message(
                    session_id=job.session_id,
                    job_id=job.job_id,
                    user_message=title_message,
                )
        except Exception as error:  # noqa: BLE001
            await self._bus.publish(
                job_id=job.job_id,
                event_type=EventType.ERROR,
                payload={
                    "error": f"会话自动命名失败: {error}",
                    "phase": "session_auto_title",
                },
                agent_id="session_title_service",
            )

        result = await self._agent_execution_service.run_step(
            job.session_id,
            job.message,
            agent_id=job.agent_id,
            job_id=job.job_id,
            message_id=job.message_id,
            attachments=job.attachments,
            message_created_at=job.message_created_at,
            message_metadata=job.message_metadata,
        )

        result_text = result if isinstance(result, str) else str(result)

        job.result = result_text
        transition_job_status(job, JobStatus.completed)
        job.progress = 100

        await self._bus.publish(
            job_id=job.job_id,
            event_type=EventType.JOB_COMPLETED,
            payload={"result": result_text},
            agent_id="job_service",
        )
        return result_text

    async def fail(self, job: JobRuntimeStateProtocol, error: Exception) -> None:
        transition_job_status(
            job,
            JobStatus.failed,
            error_message=str(error),
        )
        await self._bus.publish(
            job_id=job.job_id,
            event_type=EventType.JOB_FAILED,
            payload={"error": str(error)},
            agent_id="job_service",
        )

        
