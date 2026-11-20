from __future__ import annotations

import asyncio

import pytest

from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.service import JobState
from tests.support.job_thread_fixture import (
    job_fixture_execution_binding,
    job_fixture_thread_id,
)
from tests.unit.services.business.job.ports import enqueue_job


class FakeJobEventBus:
    async def publish(self, **_kwargs: object) -> None:
        return None


class FakeJobExecutor:
    async def run(self, _state: object) -> str:
        return "ok"


@pytest.mark.asyncio
async def test_stop_session_jobs_removes_running_and_queued_jobs(job_service_factory,
    session_lifecycle_guard,
):
    service = job_service_factory(
        job_event_bus=FakeJobEventBus(),
        job_executor=FakeJobExecutor(),
        session_lifecycle_guard=session_lifecycle_guard,
    )
    service._jobs.clear()
    session_id = "ses_cleanup"

    async def never_finish() -> None:
        await asyncio.sleep(60)

    running_task = asyncio.create_task(never_finish())
    execution_settling, release_execution = asyncio.Event(), asyncio.Event()

    async def finish_execution() -> None:
        execution_settling.set()
        await release_execution.wait()

    execution_task = asyncio.create_task(finish_execution())
    running_job = JobState(
        job_id="job_running",
        session_id=session_id,
        message="running",
        message_id="msg_running",
        message_created_at="2026-07-14T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.running,
        task=running_task, execution_binding=job_fixture_execution_binding("job_running"), thread_id=job_fixture_thread_id(session_id),
        execution_task=execution_task,
    )
    queued_job = JobState(
        job_id="job_queued",
        session_id=session_id,
        message="queued",
        message_id="msg_queued",
        message_created_at="2026-07-14T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
        delivery_policy="after_turn", execution_binding=job_fixture_execution_binding("job_queued"), thread_id=job_fixture_thread_id(session_id),
    )
    other_job = JobState(
        job_id="job_other",
        session_id="ses_other",
        message="other",
        message_id="msg_other",
        message_created_at="2026-07-14T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued, execution_binding=job_fixture_execution_binding("job_other"), thread_id=job_fixture_thread_id("ses_other"),
    )
    service._jobs[running_job.job_id] = running_job
    service._jobs[queued_job.job_id] = queued_job
    service._jobs[other_job.job_id] = other_job
    service._thread_current_job[(session_id, job_fixture_thread_id(session_id))] = running_job.job_id
    enqueue_job(service, queued_job, "after_turn")

    stopping = asyncio.create_task(service.run_session_control_operation(
        session_id, lambda: service.stop_session_jobs(session_id),
        deadline=asyncio.get_running_loop().time() + 30,
    ))
    try:
        await execution_settling.wait()
        await asyncio.gather(running_task, return_exceptions=True)
        assert not stopping.done()
    finally:
        release_execution.set()
        deleted_count = await stopping

    assert deleted_count == 2
    assert execution_task.done()
    assert running_task.cancelled()
    assert running_job.job_id not in service._jobs
    assert queued_job.job_id not in service._jobs
    assert other_job.job_id in service._jobs
    assert all(key[0] != session_id for key in service._thread_current_job)
    assert service._pending_queue.queued_job_ids(session_id) == ()
