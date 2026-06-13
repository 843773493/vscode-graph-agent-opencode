from __future__ import annotations

import asyncio

import pytest

from app.core.job_event_bus import JobEventBus
from app.schemas.public_v2.common import ControlAction, JobStatus
from app.schemas.public_v2.job import JobControlRequest
from app.services.business.job_service import JobService, JobState


class _DummyJobExecutor:
    async def run(self, job):
        return "ok"


def create_job_service() -> JobService:
    return JobService(job_event_bus=JobEventBus(), job_executor=_DummyJobExecutor())


class DummyTask:
    def __init__(self, done: bool = False):
        self._done = done
        self.cancel_called = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancel_called = True
        self._done = True


@pytest.mark.asyncio
async def test_job_control_pause_cancels_running_task(monkeypatch):
    service = create_job_service()
    service._jobs = {}

    job = JobState(
        job_id="job_test_pause",
        session_id="session_test",
        status=JobStatus.running,
        task=DummyTask(),
    )
    service._jobs[job.job_id] = job

    result = await service.control(
        job.job_id,
        JobControlRequest(action=ControlAction.pause),
    )

    assert job.status == JobStatus.paused
    assert result.status == JobStatus.paused
    assert job.task.cancel_called is True


@pytest.mark.asyncio
async def test_job_control_resume_restarts_completed_pause(monkeypatch):
    service = create_job_service()
    service._jobs = {}

    job = JobState(
        job_id="job_test_resume",
        session_id="session_test",
        status=JobStatus.paused,
        task=DummyTask(done=True),
        message="resume me",
    )
    service._jobs[job.job_id] = job

    created_tasks: list[asyncio.Future] = []

    def fake_create_task(coro):
        created_tasks.append(coro)
        coro.close()

        class _DummyCreatedTask:
            def done(self) -> bool:
                return False

        return _DummyCreatedTask()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    result = await service.control(
        job.job_id,
        JobControlRequest(action=ControlAction.resume, params={"reason": "continue"}),
    )

    assert job.status == JobStatus.running
    assert result.status == JobStatus.running
    assert len(created_tasks) == 1


@pytest.mark.asyncio
async def test_job_control_cancel_requests_task_cancel(monkeypatch):
    service = create_job_service()
    service._jobs = {}

    task = DummyTask()
    job = JobState(
        job_id="job_test_cancel",
        session_id="session_test",
        status=JobStatus.running,
        task=task,
    )
    service._jobs[job.job_id] = job

    result = await service.control(
        job.job_id,
        JobControlRequest(action=ControlAction.cancel),
    )

    assert job.status == JobStatus.cancelling
    assert result.status == JobStatus.cancelling
    assert task.cancel_called is True


@pytest.mark.asyncio
async def test_start_job_queues_same_session_until_previous_finishes(monkeypatch):
    service = create_job_service()
    service._jobs = {}
    service._session_current_job = {}
    service._session_waiting_jobs = {}

    started_jobs: list[str] = []

    def fake_start_job_task(job):
        started_jobs.append(job.job_id)
        job.task = DummyTask(done=False)

    monkeypatch.setattr(service, "_start_job_task", fake_start_job_task)

    session_id = "session_queue_test"
    first_job_id = await service.start_job(session_id, "first")
    second_job_id = await service.start_job(session_id, "second")

    assert started_jobs == [first_job_id]
    assert service._session_current_job[session_id] == first_job_id
    assert list(service._session_waiting_jobs[session_id]) == [second_job_id]
    assert service._jobs[second_job_id].status == JobStatus.queued

    first_job = service._jobs[first_job_id]
    first_job.status = JobStatus.completed

    await service._schedule_next_job_if_needed(first_job)

    assert started_jobs == [first_job_id, second_job_id]
    assert service._session_current_job[session_id] == second_job_id
