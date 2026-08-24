from __future__ import annotations

import asyncio
import contextvars

import pytest
from langchain_core.runnables import RunnableLambda

from app.core.job_event_bus import JobEventBus
from app.schemas.public_v2.common import ControlAction, JobStatus
from app.schemas.public_v2.job import JobControlRequest
from app.services.business.job.service import JobDrainBlocker, JobService, JobState


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
async def test_multiple_session_storage_operation_rejects_any_active_session() -> None:
    service = create_job_service()
    service._session_current_job["ses_active"] = "job_active"
    operation_called = False

    async def move_storage() -> None:
        nonlocal operation_called
        operation_called = True

    with pytest.raises(RuntimeError, match="不能移动物理存储"):
        await service.run_sessions_idle_operation(
            ["ses_idle", "ses_active"],
            move_storage,
        )

    assert operation_called is False


@pytest.mark.asyncio
async def test_storage_move_rejects_message_preparation_window() -> None:
    service = create_job_service()
    preparation_started = asyncio.Event()
    release_preparation = asyncio.Event()

    async def prepare_message() -> None:
        preparation_started.set()
        await release_preparation.wait()

    preparation_task = asyncio.create_task(
        service.run_session_preparation("ses_preparing", prepare_message)
    )
    await preparation_started.wait()

    with pytest.raises(RuntimeError, match="正在准备持久化消息"):
        await service.run_sessions_idle_operation(
            ["ses_preparing"],
            lambda: asyncio.sleep(0),
        )

    release_preparation.set()
    await preparation_task


@pytest.mark.asyncio
async def test_delete_tombstone_blocks_new_session_writes() -> None:
    service = create_job_service()
    delete_started = asyncio.Event()
    release_delete = asyncio.Event()

    async def delete_storage() -> str:
        delete_started.set()
        await release_delete.wait()
        return "deleted"

    delete_task = asyncio.create_task(
        service.run_session_delete_operation("ses_delete", delete_storage)
    )
    await delete_started.wait()

    with pytest.raises(RuntimeError, match="正在删除"):
        await service.run_session_preparation(
            "ses_delete",
            lambda: asyncio.sleep(0),
        )

    release_delete.set()
    assert await delete_task == "deleted"


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
async def test_job_control_resume_waits_for_paused_task_to_finish() -> None:
    started = asyncio.Event()

    class _BlockingJobExecutor:
        async def run(self, job):
            del job
            started.set()
            await asyncio.Future()

    service = JobService(
        job_event_bus=JobEventBus(),
        job_executor=_BlockingJobExecutor(),
    )
    session_id = "session_pause_resume"
    job = JobState(
        job_id="job_pause_resume",
        session_id=session_id,
        message="pause and resume",
        message_id="msg_pause_resume",
        message_created_at="2026-08-09T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.queued,
        delivery_policy="after_turn",
    )
    service._jobs[job.job_id] = job
    service._pending_queue.append(session_id, job.job_id, "after_turn")
    assert await service._start_next_pending(session_id, boundary="idle") is True
    await started.wait()

    await service.control(
        job.job_id,
        JobControlRequest(action=ControlAction.pause),
    )
    result = await service.control(
        job.job_id,
        JobControlRequest(action=ControlAction.resume),
    )

    assert result.status == JobStatus.running
    assert job.status == JobStatus.running
    assert service._session_current_job[session_id] == job.job_id
    assert job.task is not None
    resumed_task = job.task

    await service.control(
        job.job_id,
        JobControlRequest(action=ControlAction.cancel),
    )
    await asyncio.gather(resumed_task, return_exceptions=True)


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
@pytest.mark.parametrize(
    "action",
    [ControlAction.pause, ControlAction.resume, ControlAction.cancel],
)
async def test_job_control_rejects_terminal_job(action: ControlAction) -> None:
    service = create_job_service()
    job = JobState(
        job_id="job_terminal_control",
        session_id="session_test",
        message="terminal",
        message_id="msg_terminal",
        message_created_at="2026-08-09T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.completed,
        task=DummyTask(done=True),
    )
    service._jobs[job.job_id] = job

    with pytest.raises(ValueError):
        await service.control(job.job_id, JobControlRequest(action=action))

    assert job.status == JobStatus.completed


@pytest.mark.asyncio
async def test_job_control_rejects_unimplemented_action() -> None:
    service = create_job_service()
    job = JobState(
        job_id="job_unimplemented_control",
        session_id="session_test",
        message="unsupported",
        message_id="msg_unsupported",
        message_created_at="2026-08-09T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.running,
        task=DummyTask(),
    )
    service._jobs[job.job_id] = job

    with pytest.raises(ValueError, match="尚未实现"):
        await service.control(
            job.job_id,
            JobControlRequest(action=ControlAction.skip),
        )

    assert job.status == JobStatus.running


@pytest.mark.asyncio
async def test_job_control_rejects_pause_for_interrupt_pending_job() -> None:
    service = create_job_service()
    job = JobState(
        job_id="job_interrupt_pending_pause",
        session_id="session_test",
        message="interrupt pending",
        message_id="msg_interrupt_pending_pause",
        message_created_at="2026-08-09T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.interrupt_pending,
        task=DummyTask(),
    )
    service._jobs[job.job_id] = job

    with pytest.raises(ValueError, match="只有 running、streaming 或 waiting_input"):
        await service.control(
            job.job_id,
            JobControlRequest(action=ControlAction.pause),
        )

    assert job.status == JobStatus.interrupt_pending


@pytest.mark.asyncio
async def test_force_interrupt_skips_job_finished_after_blocker_snapshot(monkeypatch):
    service = create_job_service()
    job = JobState(
        job_id="job_finished_after_snapshot",
        session_id="session_test",
        message="finished",
        message_id="msg_finished_after_snapshot",
        message_created_at="2026-08-09T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.completed,
        task=DummyTask(done=True),
    )
    service._jobs[job.job_id] = job

    async def stale_blockers() -> list[JobDrainBlocker]:
        return [
            JobDrainBlocker(
                job_id=job.job_id,
                session_id=job.session_id,
                status=JobStatus.running,
                phase="text",
                tool_names=(),
            )
        ]

    monkeypatch.setattr(service, "drain_blockers", stale_blockers)

    assert await service.force_interrupt_active(reason="runtime restart") == 0
    assert job.status == JobStatus.completed


@pytest.mark.asyncio
async def test_job_control_cancel_queued_job_removes_it_from_queue(monkeypatch):
    service = create_job_service()

    def fake_start_job_task(job):
        job.task = DummyTask()

    monkeypatch.setattr(service, "_start_job_task", fake_start_job_task)
    session_id = "session_cancel_queued"
    await service.start_job(
        session_id,
        "active",
        message_id="msg_active",
        message_created_at="2026-08-09T00:00:00+00:00",
    )
    queued = await service.start_job(
        session_id,
        "queued",
        message_id="msg_queued",
        message_created_at="2026-08-09T00:00:01+00:00",
    )

    result = await service.control(
        queued.job_id,
        JobControlRequest(action=ControlAction.cancel),
    )

    assert result.status == JobStatus.cancelled
    assert service._jobs[queued.job_id].status == JobStatus.cancelled
    assert service._pending_queue.ids(session_id) == ()


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
    assert service._jobs[second_job_id].status == JobStatus.running


@pytest.mark.asyncio
async def test_boundary_notification_keeps_fifo_head_and_records_waiting_reason(
    monkeypatch,
):
    service = create_job_service()
    monkeypatch.setattr(
        service,
        "_start_job_task",
        lambda job: setattr(job, "task", DummyTask(done=False)),
    )
    session_id = "session_boundary_dispatch"
    await service.start_job(
        session_id,
        "active",
        message_id="msg_active",
        message_created_at="2026-07-27T00:00:00+00:00",
    )
    queued = await service.start_job(
        session_id,
        "等待中断",
        message_id="msg_queued",
        message_created_at="2026-07-27T00:00:01+00:00",
        delivery_policy="after_interrupt",
    )

    snapshot = await service.notify_boundary(
        session_id,
        "after_tool_result",
        tool_result_available=True,
    )

    request = next(item for item in snapshot.requests if item.message_id == "msg_queued")
    assert request.status == "queued"
    assert request.waiting_reason is None
    assert service._pending_queue.ids(session_id) == (queued.job_id,)
