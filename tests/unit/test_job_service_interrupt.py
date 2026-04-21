from __future__ import annotations

import asyncio

import pytest

from app.schemas.common import ControlAction, JobStatus
from app.schemas.job import JobControlRequest
from app.services.job_service import JobService, JobState


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
    service = JobService.get_instance()
    monkeypatch.setattr(JobService, "_jobs", {})

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
    service = JobService.get_instance()
    monkeypatch.setattr(JobService, "_jobs", {})

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
        JobControlRequest(action=ControlAction.resume, input={"reason": "continue"}),
    )

    assert job.status == JobStatus.running
    assert result.status == JobStatus.running
    assert len(created_tasks) == 1


@pytest.mark.asyncio
async def test_job_control_cancel_requests_task_cancel(monkeypatch):
    service = JobService.get_instance()
    monkeypatch.setattr(JobService, "_jobs", {})

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
