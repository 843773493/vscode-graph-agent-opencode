from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.domain.itemized.execution_admission import (
    ExecutionBinding,
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.runtime_state import JobRuntimeState
from app.services.orchestration.job_execution_service import JobExecutionService


class _StepExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run_step(
        self,
        session_id: str,
        message: str,
        **kwargs: object,
    ) -> str:
        self.calls.append({"session_id": session_id, "message": message, **kwargs})
        return "已执行"


class _JobEventBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish(self, **event: object) -> None:
        self.events.append(event)


class _SessionTitleService:
    async def maybe_auto_title_before_first_message(self, **_kwargs: object) -> None:
        return None


@pytest.fixture
def execution_service() -> tuple[JobExecutionService, _StepExecutor]:
    step_executor = _StepExecutor()
    service = JobExecutionService(
        agent_execution_service=step_executor,  # type: ignore[arg-type]
        message_service=MagicMock(),
        job_event_bus=_JobEventBus(),  # type: ignore[arg-type]
        session_title_service=_SessionTitleService(),  # type: ignore[arg-type]
    )
    return service, step_executor


@pytest.mark.parametrize(
    "execution_binding",
    [
        UserExecutionIdentity(
            job_id="job_runtime",
            turn_id="turn_runtime",
            admission_idempotency_key="adm_runtime",
        ),
        InternalExecutionIntent(
            message_id="msg_runtime",
            execution_id="intr_runtime",
            admission_idempotency_key="adm_internal_runtime",
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_forwards_full_execution_binding_and_message_identity(
    execution_service: tuple[JobExecutionService, _StepExecutor],
    execution_binding: ExecutionBinding,
) -> None:
    service, step_executor = execution_service
    job = JobRuntimeState(
        job_id="job_runtime",
        session_id="ses_runtime",
        thread_id="thr_runtime",
        message="原始消息",
        agent_id="agent_runtime",
        message_id="msg_runtime",
        message_created_at="2026-10-04T00:00:00+00:00",
        execution_binding=execution_binding,
        message_metadata={"source": "ingress"},
    )

    result = await service.run(job)

    assert result == "已执行"
    assert job.status == JobStatus.completed
    assert step_executor.calls == [
        {
            "session_id": "ses_runtime",
            "message": "原始消息",
            "thread_id": "thr_runtime",
            "agent_id": "agent_runtime",
            "job_id": "job_runtime",
            "message_id": "msg_runtime",
            "execution_binding": execution_binding,
            "attachments": [],
            "message_created_at": "2026-10-04T00:00:00+00:00",
            "message_metadata": {"source": "ingress"},
            "progress_reporter": None,
            "protected_operations_settled": job.protected_operations_settled,
        }
    ]
