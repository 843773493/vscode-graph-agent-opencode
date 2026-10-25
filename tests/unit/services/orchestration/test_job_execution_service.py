from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.job_event_bus import EventType
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
@pytest.mark.parametrize("resume_checkpoint_id", [None, "original-checkpoint"])
@pytest.mark.asyncio
async def test_run_forwards_full_execution_binding_and_message_identity(
    execution_service: tuple[JobExecutionService, _StepExecutor],
    execution_binding: ExecutionBinding,
    resume_checkpoint_id: str | None,
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
        resume_checkpoint_id=resume_checkpoint_id,
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
            "resume_checkpoint_id": resume_checkpoint_id,
            "attachments": [],
            "message_created_at": "2026-10-04T00:00:00+00:00",
            "message_metadata": {"source": "ingress"},
            "progress_reporter": None,
            "protected_operations_settled": job.protected_operations_settled,
        }
    ]


async def test_auto_title_io_failure_preserves_execution_and_original_diagnostic(
    execution_service: tuple[JobExecutionService, _StepExecutor],
    tmp_path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, executor = execution_service
    missing = tmp_path / "private-title-input"
    with pytest.raises(FileNotFoundError) as raised:
        missing.read_bytes()
    error = raised.value
    monkeypatch.setattr(service._session_title_service, "maybe_auto_title_before_first_message",
                        AsyncMock(side_effect=error))
    job = JobRuntimeState(
        job_id="job_title", session_id="ses_title", thread_id="thr_title", message="消息",
        agent_id="default", message_id="msg_title", message_created_at="2026-10-25T00:00:00+00:00",
        execution_binding=UserExecutionIdentity(job_id="job_title", turn_id="turn_title",
                                                admission_idempotency_key="adm_title"),
    )
    assert await service.run(job) == "已执行"
    assert job.status == JobStatus.completed and len(executor.calls) == 1
    failure, = [event["payload"] for event in service._bus.events if event["event_type"] == EventType.ERROR]
    assert failure == {
        "error": f"会话自动命名失败: 执行系统 I/O 失败: error=FileNotFoundError, errno={error.errno}",
        "phase": "session_auto_title",
    }
    assert str(missing) in caplog.text
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
