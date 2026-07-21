from __future__ import annotations

import asyncio
import contextvars

import pytest
from langchain_core.runnables import RunnableLambda

from app.core.job_event_bus import JobEventBus
from app.schemas.public_v2.common import ControlAction, JobStatus
from app.schemas.public_v2.job import JobControlRequest
from app.services.business.job.service import JobService, JobState


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
async def test_session_idle_operation_is_atomic_with_job_admission() -> None:
    service = create_job_service()
    session_id = "session_compaction_lock"
    operation_started = asyncio.Event()
    release_operation = asyncio.Event()
    admission_finished = asyncio.Event()

    async def checkpoint_operation() -> str:
        operation_started.set()
        await release_operation.wait()
        return "scheduled"

    async def competing_admission() -> None:
        async with service._dispatch_lock:
            service._session_current_job[session_id] = "job_after_compaction"
        admission_finished.set()

    compact_task = asyncio.create_task(
        service.run_session_idle_operation(session_id, checkpoint_operation)
    )
    await operation_started.wait()
    admission_task = asyncio.create_task(competing_admission())
    await asyncio.sleep(0)
    assert admission_finished.is_set() is False

    release_operation.set()
    assert await compact_task == "scheduled"
    await admission_task
    assert admission_finished.is_set() is True

    with pytest.raises(RuntimeError, match="不能修改 checkpoint"):
        await service.run_session_idle_operation(
            session_id,
            checkpoint_operation,
        )


@pytest.mark.asyncio
async def test_job_control_pause_cancels_running_task(monkeypatch):
    service = create_job_service()
    service._jobs = {}

    job = JobState(
        job_id="job_test_pause",
        session_id="session_test",
        message="pause me",
        message_id="msg_pause",
        message_created_at="2026-07-14T00:00:00+00:00",
        agent_id="default",
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
        message_id="msg_resume",
        message_created_at="2026-07-14T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.paused,
        task=DummyTask(done=True),
        message="resume me",
    )
    service._jobs[job.job_id] = job

    started_jobs: list[str] = []

    def fake_start_job_task(target_job):
        started_jobs.append(target_job.job_id)
        target_job.task = DummyTask()

    monkeypatch.setattr(service, "_start_job_task", fake_start_job_task)

    result = await service.control(
        job.job_id,
        JobControlRequest(action=ControlAction.resume, params={"reason": "continue"}),
    )

    assert job.status == JobStatus.running
    assert result.status == JobStatus.running
    assert started_jobs == [job.job_id]


@pytest.mark.asyncio
async def test_job_task_starts_in_fresh_context():
    inherited_value: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "inherited_value",
        default=None,
    )
    observed_values: list[str | None] = []

    class _ContextRecordingJobExecutor:
        async def run(self, job):
            del job
            observed_values.append(inherited_value.get())
            return "ok"

    service = JobService(
        job_event_bus=JobEventBus(),
        job_executor=_ContextRecordingJobExecutor(),
    )
    service._jobs = {}
    context_token = inherited_value.set("sender_context")
    try:
        dispatch = await service.start_job(
            "session_context_isolation",
            "run independently",
            message_id="msg_context_isolation",
            message_created_at="2026-07-17T00:00:00+00:00",
        )
        job_id = dispatch.job_id
        job_task = service._jobs[job_id].task
        assert job_task is not None
        await job_task
    finally:
        inherited_value.reset(context_token)

    assert observed_values == [None]


@pytest.mark.asyncio
async def test_cross_session_job_does_not_leak_langchain_events_to_sender():
    async def target_runnable_function(value: str) -> str:
        return value

    target_runnable = RunnableLambda(target_runnable_function).with_config(
        run_name="target_session_job"
    )

    class _TargetJobExecutor:
        async def run(self, job):
            del job
            async for _event in target_runnable.astream_events(
                "target",
                version="v2",
            ):
                pass
            return "ok"

    service = JobService(
        job_event_bus=JobEventBus(),
        job_executor=_TargetJobExecutor(),
    )
    service._jobs = {}

    async def sender_runnable_function(value: str) -> str:
        dispatch = await service.start_job(
            "session_target",
            "target message",
            message_id="msg_target",
            message_created_at="2026-07-17T00:00:00+00:00",
        )
        job_id = dispatch.job_id
        target_task = service._jobs[job_id].task
        assert target_task is not None
        await target_task
        return value

    sender_runnable = RunnableLambda(sender_runnable_function).with_config(
        run_name="sender_session_job"
    )
    observed_names: list[str] = []
    async for event in sender_runnable.astream_events("sender", version="v2"):
        observed_names.append(event["name"])

    assert "sender_session_job" in observed_names
    assert "target_session_job" not in observed_names


@pytest.mark.asyncio
async def test_job_control_cancel_requests_task_cancel(monkeypatch):
    service = create_job_service()
    service._jobs = {}

    task = DummyTask()
    job = JobState(
        job_id="job_test_cancel",
        session_id="session_test",
        message="cancel me",
        message_id="msg_cancel",
        message_created_at="2026-07-14T00:00:00+00:00",
        agent_id="default",
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
    started_jobs: list[str] = []

    def fake_start_job_task(job):
        started_jobs.append(job.job_id)
        job.task = DummyTask(done=False)

    monkeypatch.setattr(service, "_start_job_task", fake_start_job_task)

    session_id = "session_queue_test"
    first_dispatch = await service.start_job(
        session_id,
        "first",
        message_id="msg_first",
        message_created_at="2026-07-14T00:00:00+00:00",
    )
    second_dispatch = await service.start_job(
        session_id,
        "second",
        message_id="msg_second",
        message_created_at="2026-07-14T00:00:01+00:00",
    )
    third_dispatch = await service.start_job(
        session_id,
        "third",
        message_id="msg_third",
        message_created_at="2026-07-14T00:00:02+00:00",
    )
    first_job_id = first_dispatch.job_id
    second_job_id = second_dispatch.job_id
    third_job_id = third_dispatch.job_id

    assert started_jobs == [first_job_id]
    assert first_dispatch.job_status == "running"
    assert first_dispatch.active_job_id == first_job_id
    assert first_dispatch.blocked_by_job_id is None
    assert first_dispatch.queued_jobs_ahead == 0
    assert first_dispatch.queued_job_count == 0
    assert first_dispatch.pending_job_count == 1
    assert second_dispatch.job_status == "queued"
    assert second_dispatch.active_job_id == first_job_id
    assert second_dispatch.blocked_by_job_id == first_job_id
    assert second_dispatch.queued_jobs_ahead == 0
    assert second_dispatch.queued_job_count == 1
    assert second_dispatch.pending_job_count == 2
    assert third_dispatch.job_status == "queued"
    assert third_dispatch.active_job_id == first_job_id
    assert third_dispatch.blocked_by_job_id == first_job_id
    assert third_dispatch.queued_jobs_ahead == 1
    assert third_dispatch.queued_job_count == 2
    assert third_dispatch.pending_job_count == 3
    assert service._session_current_job[session_id] == first_job_id
    assert list(service._pending_queue.ids(session_id)) == [
        second_job_id,
        third_job_id,
    ]
    assert service._jobs[second_job_id].status == JobStatus.queued
    assert service._jobs[third_job_id].status == JobStatus.queued

    first_job = service._jobs[first_job_id]
    first_job.status = JobStatus.completed

    await service._schedule_next_job_if_needed(first_job)

    assert started_jobs == [first_job_id, second_job_id]
    assert service._session_current_job[session_id] == second_job_id
    assert list(service._pending_queue.ids(session_id)) == [third_job_id]
