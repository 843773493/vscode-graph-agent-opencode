from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.background_task_registry import BackgroundTaskRegistry
from app.core.identifier import create_prefixed_id
from app.schemas.event import JobStartedEvent, JobStartedPayload
from app.schemas.public_v2.common import JobStatus
from app.services.business.job.service import (
    JobAdmissionClosedError,
    JobService,
    JobState,
)
from app.services.infrastructure.background_task_history_store import (
    BackgroundTaskHistoryStore,
)
from app.services.infrastructure.runtime_service import RuntimeService
from app.services.infrastructure.trace_event_store import TraceEventStore


class RecordingJobEventBus:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def publish(self, **event: object) -> None:
        self.events.append(event)


class NeverFinishExecutor:
    async def run(self, _state: object) -> str:
        await asyncio.Future()
        raise AssertionError("不可达")


def build_runtime(tmp_path: Path) -> tuple[RuntimeService, JobService]:
    sessions_dir = tmp_path / ".boxteam" / "sessions"
    bus = RecordingJobEventBus()
    jobs = JobService(
        job_event_bus=bus,
        job_executor=NeverFinishExecutor(),
    )
    runtime = RuntimeService(
        job_service=jobs,
        background_task_registry=BackgroundTaskRegistry(
            history_store=BackgroundTaskHistoryStore(sessions_dir=sessions_dir)
        ),
        trace_event_store=TraceEventStore(sessions_dir=sessions_dir),
    )
    return runtime, jobs


@pytest.mark.asyncio
async def test_drain_closes_admission_and_cancel_reopens_it(tmp_path: Path) -> None:
    runtime, jobs = build_runtime(tmp_path)

    draining = await runtime.begin_drain()

    assert draining.lifecycle_state == "draining"
    assert not draining.accepting_jobs
    with pytest.raises(JobAdmissionClosedError):
        jobs.assert_accepting_jobs()

    ready = await runtime.cancel_drain()

    assert ready.lifecycle_state == "ready"
    assert ready.accepting_jobs
    jobs.assert_accepting_jobs()


@pytest.mark.asyncio
async def test_force_interrupt_persists_event_and_cancels_job(tmp_path: Path) -> None:
    runtime, jobs = build_runtime(tmp_path)
    task = asyncio.create_task(asyncio.sleep(60))
    job = JobState(
        job_id="job_running",
        session_id="ses_runtime",
        message="运行中",
        message_id="msg_runtime",
        message_created_at=datetime.now(timezone.utc).isoformat(),
        agent_id="default",
        status=JobStatus.running,
        task=task,
    )
    jobs._jobs[job.job_id] = job
    jobs._session_current_job[job.session_id] = job.job_id

    draining = await runtime.begin_drain()
    forced = await runtime.force_interrupt()

    assert [blocker.resource_id for blocker in draining.blockers] == ["job_running"]
    assert forced.lifecycle_state == "stopping"
    assert forced.interrupted_resources == 1
    assert task.cancelled()
    assert job.status == JobStatus.cancelled
    assert job.error_message == "Gateway 显式强制重启 Workspace API"


@pytest.mark.asyncio
async def test_startup_reconciles_job_without_terminal_event(tmp_path: Path) -> None:
    runtime, _ = build_runtime(tmp_path)
    store = runtime._trace_event_store
    now = datetime.now(timezone.utc)
    await store.append(
        "ses_stale",
        JobStartedEvent(
            event_id=create_prefixed_id("evt"),
            job_id="job_stale",
            timestamp=now,
            payload=JobStartedPayload(),
        ),
    )

    reconciled = await runtime.reconcile_stale_executions()
    events = store.read_events("ses_stale")

    assert reconciled == 1
    assert events[-1].type == "session_interrupted"
    assert events[-1].payload.phase == "process_exit"
