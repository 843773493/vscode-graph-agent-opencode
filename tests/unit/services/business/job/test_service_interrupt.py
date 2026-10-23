from __future__ import annotations

import asyncio
import contextvars
import sqlite3
from datetime import UTC, datetime

import pytest
from langchain_core.runnables import RunnableLambda

from app.core.job_event_bus import JobEventBus
from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.execution_queue import ExecutionQueueInput
from app.schemas.internal_v2.common import ControlAction, JobStatus, MessageRole
from app.schemas.internal_v2.job import JobControlRequest
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.turn import TurnHistoryLoadRequest
from app.services.business.job.lifecycle import transition_job_status
from app.services.business.job.service import JobDrainBlocker, JobService, JobState
from app.services.infrastructure.pending_request_store import PendingRequestStore
from app.services.infrastructure.rollout_context.checkpoint.reader import (
    RolloutContextReader,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_history_reader import RolloutHistoryReader
from tests.support.job_thread_fixture import (
    job_fixture_execution_binding,
    job_fixture_thread_id,
)
from tests.support.message_service import build_message_service
from tests.unit.services.business.job.ports import enqueue_job


class _DummyJobExecutor:
    async def run(self, job):
        return "ok"


def create_job_service(session_lifecycle_guard, *, job_service_factory) -> JobService:
    return job_service_factory(
        job_event_bus=JobEventBus(),
        job_executor=_DummyJobExecutor(),
        session_lifecycle_guard=session_lifecycle_guard,
    )


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
async def test_session_idle_operation_is_atomic_with_job_admission(job_service_factory,
    session_lifecycle_guard,
) -> None:
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
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
            service._thread_current_job[(session_id, job_fixture_thread_id(session_id))] = "job_after_compaction"
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
async def test_multiple_session_storage_operation_rejects_any_active_session(job_service_factory,
    session_lifecycle_guard,
) -> None:
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    service._thread_current_job[("ses_active", job_fixture_thread_id("ses_active"))] = "job_active"
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
async def test_storage_move_rejects_message_preparation_window(job_service_factory,
    session_lifecycle_guard,
) -> None:
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
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
async def test_delete_tombstone_blocks_new_session_writes(job_service_factory,
    session_lifecycle_guard,
) -> None:
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
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
async def test_job_control_pause_cancels_running_task(job_service_factory,
    monkeypatch, session_lifecycle_guard
):
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    service._jobs = {}

    job = JobState(
        job_id="job_test_pause",
        session_id="session_test",
        message="pause me",
        message_id="msg_pause",
        message_created_at="2026-07-14T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.running,
        task=DummyTask(), execution_binding=job_fixture_execution_binding("job_test_pause"), thread_id=job_fixture_thread_id("session_test"),
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
async def test_job_control_resume_waits_for_paused_task_to_finish(job_service_factory,
    session_lifecycle_guard,
) -> None:
    started = asyncio.Event()

    class _BlockingJobExecutor:
        async def run(self, job):
            del job
            started.set()
            await asyncio.Future()

    service = job_service_factory(
        job_event_bus=JobEventBus(),
        job_executor=_BlockingJobExecutor(),
        session_lifecycle_guard=session_lifecycle_guard,
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
        delivery_policy="after_turn", execution_binding=job_fixture_execution_binding("job_pause_resume"), thread_id=job_fixture_thread_id(session_id),
    )
    service._jobs[job.job_id] = job
    enqueue_job(service, job, "after_turn")
    assert await service._start_next_pending(session_id, thread_id=job_fixture_thread_id(session_id), boundary="idle") is True
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
    assert service._thread_current_job[(session_id, job_fixture_thread_id(session_id))] == job.job_id
    assert job.task is not None
    resumed_task = job.task

    await service.control(
        job.job_id,
        JobControlRequest(action=ControlAction.cancel),
    )
    await asyncio.gather(resumed_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_job_control_resume_restarts_completed_pause(job_service_factory,
    monkeypatch, session_lifecycle_guard
):
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    service._jobs = {}

    job = JobState(
        job_id="job_test_resume",
        session_id="session_test",
        message_id="msg_resume",
        message_created_at="2026-07-14T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.paused,
        task=DummyTask(done=True),
        message="resume me", execution_binding=job_fixture_execution_binding("job_test_resume"), thread_id=job_fixture_thread_id("session_test"),
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
async def test_job_task_starts_in_fresh_context(job_service_factory, session_lifecycle_guard):
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

    service = job_service_factory(
        job_event_bus=JobEventBus(),
        job_executor=_ContextRecordingJobExecutor(),
        session_lifecycle_guard=session_lifecycle_guard,
    )
    service._jobs = {}
    context_token = inherited_value.set("sender_context")
    try:
        dispatch = await service.start_job(
            "session_context_isolation",
            "run independently", thread_id=job_fixture_thread_id("session_context_isolation"),
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
async def test_cross_session_job_does_not_leak_langchain_events_to_sender(job_service_factory,
    session_lifecycle_guard,
):
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

    service = job_service_factory(
        job_event_bus=JobEventBus(),
        job_executor=_TargetJobExecutor(),
        session_lifecycle_guard=session_lifecycle_guard,
    )
    service._jobs = {}

    async def sender_runnable_function(value: str) -> str:
        dispatch = await service.start_job(
            "session_target",
            "target message", thread_id=job_fixture_thread_id("session_target"),
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
async def test_job_control_cancel_requests_task_cancel(job_service_factory,
    monkeypatch, session_lifecycle_guard
):
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
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
        task=task, execution_binding=job_fixture_execution_binding("job_test_cancel"), thread_id=job_fixture_thread_id("session_test"),
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
async def test_job_control_rejects_terminal_job(job_service_factory,
    action: ControlAction, session_lifecycle_guard
) -> None:
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    job = JobState(
        job_id="job_terminal_control",
        session_id="session_test",
        message="terminal",
        message_id="msg_terminal",
        message_created_at="2026-08-09T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.completed,
        task=DummyTask(done=True), execution_binding=job_fixture_execution_binding("job_terminal_control"), thread_id=job_fixture_thread_id("session_test"),
    )
    service._jobs[job.job_id] = job

    with pytest.raises(ValueError):
        await service.control(job.job_id, JobControlRequest(action=action))

    assert job.status == JobStatus.completed


@pytest.mark.asyncio
async def test_job_control_rejects_unimplemented_action(job_service_factory,
    session_lifecycle_guard,
) -> None:
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    job = JobState(
        job_id="job_unimplemented_control",
        session_id="session_test",
        message="unsupported",
        message_id="msg_unsupported",
        message_created_at="2026-08-09T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.running,
        task=DummyTask(), execution_binding=job_fixture_execution_binding("job_unimplemented_control"), thread_id=job_fixture_thread_id("session_test"),
    )
    service._jobs[job.job_id] = job

    with pytest.raises(ValueError, match="尚未实现"):
        await service.control(
            job.job_id,
            JobControlRequest(action=ControlAction.skip),
        )

    assert job.status == JobStatus.running


@pytest.mark.asyncio
async def test_job_control_rejects_pause_for_interrupt_pending_job(job_service_factory,
    session_lifecycle_guard,
) -> None:
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    job = JobState(
        job_id="job_interrupt_pending_pause",
        session_id="session_test",
        message="interrupt pending",
        message_id="msg_interrupt_pending_pause",
        message_created_at="2026-08-09T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.interrupt_pending,
        task=DummyTask(), execution_binding=job_fixture_execution_binding("job_interrupt_pending_pause"), thread_id=job_fixture_thread_id("session_test"),
    )
    service._jobs[job.job_id] = job

    with pytest.raises(ValueError, match="只有 running、streaming 或 waiting_input"):
        await service.control(
            job.job_id,
            JobControlRequest(action=ControlAction.pause),
        )

    assert job.status == JobStatus.interrupt_pending


@pytest.mark.asyncio
async def test_force_interrupt_skips_job_finished_after_blocker_snapshot(job_service_factory,
    monkeypatch, session_lifecycle_guard
):
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    job = JobState(
        job_id="job_finished_after_snapshot",
        session_id="session_test",
        message="finished",
        message_id="msg_finished_after_snapshot",
        message_created_at="2026-08-09T00:00:00+00:00",
        agent_id="default",
        status=JobStatus.completed,
        task=DummyTask(done=True), execution_binding=job_fixture_execution_binding("job_finished_after_snapshot"), thread_id=job_fixture_thread_id("session_test"),
    )
    service._jobs[job.job_id] = job

    async def stale_blockers() -> list[JobDrainBlocker]:
        return [
            JobDrainBlocker(
                job_id=job.job_id,
                session_id=job.session_id,
                thread_id=job.thread_id,
                status=JobStatus.running,
                phase="text",
                tool_names=(),
            )
        ]

    monkeypatch.setattr(service, "drain_blockers", stale_blockers)

    assert await service.force_interrupt_active(reason="runtime restart") == 0
    assert job.status == JobStatus.completed


@pytest.mark.asyncio
async def test_job_control_cancel_queued_job_removes_it_from_queue(job_service_factory,
    monkeypatch, session_lifecycle_guard, tmp_path, session_bundle_factory,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019b990a9cce734bbccc60ec5321f320"
    session_bundle_factory(sessions_dir, session_id)
    thread_id = get_session_path_resolver(sessions_dir).main_thread_id(session_id)
    saver = RolloutCheckpointSaver(sessions_dir=sessions_dir)
    owner = build_message_service(sessions_dir, checkpointer=saver, workspace_root=tmp_path)
    service = job_service_factory(job_event_bus=JobEventBus(), job_executor=_DummyJobExecutor(),
                         session_lifecycle_guard=session_lifecycle_guard, input_owner=owner, terminal_status_writer=saver,
                         pending_request_store=PendingRequestStore(sessions_dir=sessions_dir, owner=saver,
                             input_reader=lambda ref: owner.accepted_job_message(ref, display=False)))

    def fake_start_job_task(job):
        job.task = DummyTask()

    monkeypatch.setattr(service, "_start_job_task", fake_start_job_task)
    async def accepted(content, message_id):
        binding = service.reserve_user_execution_identity()
        now = datetime.now(UTC)
        owner.accept_user_message(MessageDTO(message_id=message_id, session_id=session_id, thread_id=thread_id,
                                            role=MessageRole.user, content=content, created_at=now, updated_at=now), execution_identity=binding,
            queue_input=ExecutionQueueInput(execution_binding=binding, message_id=message_id, agent_id="default", provider_id=None,
                delivery_policy="after_turn", gateway_id=None, request_id=None, created_at=now.isoformat()))
        return await service.start_job(session_id, content, thread_id=thread_id, message_id=message_id,
                                       message_created_at=now.isoformat(), execution_binding=binding)
    await accepted("active", "msg_active")
    queued = await accepted("queued", "msg_queued")
    turn_id = service._jobs[queued.job_id].execution_binding.turn_id
    database = saver._storage.storage.index_path(session_id, thread_id=thread_id)
    with sqlite3.connect(database) as connection:
        original = connection.execute("SELECT turn_id, turn_ordinal, root_input_item_id FROM turn_records WHERE turn_id=?", (turn_id,)).fetchone()

    result = await service.control(
        queued.job_id,
        JobControlRequest(action=ControlAction.cancel),
    )

    assert result.status == JobStatus.cancelled
    assert service._jobs[queued.job_id].status == JobStatus.cancelled
    assert service._pending_queue.queued_job_ids(session_id) == ()
    successor = await accepted("successor", "msg_successor")
    assert service._pending_queue.queued_job_ids(session_id) == (successor.job_id,)
    assert saver.accepted_user_message(session_id, thread_id=thread_id, job_id=queued.job_id, message_id="msg_queued").content == "queued"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT turn_id,turn_ordinal,root_input_item_id FROM turn_records WHERE turn_id=?", (turn_id,)).fetchone() == original
        assert connection.execute("SELECT outcome FROM executions WHERE job_id=?", (queued.job_id,)).fetchone() == ("cancelled",)
    history = RolloutHistoryReader(RolloutContextReader(saver._storage)).load(session_id, thread_id, TurnHistoryLoadRequest(direction="tail", turns=3))
    cancelled = next(turn for turn in history.items if turn.turn_id == turn_id)
    assert cancelled.status == "cancelled" and cancelled.user_messages[0].content == "queued"


@pytest.mark.asyncio
async def test_start_job_queues_same_session_until_previous_finishes(job_service_factory,
    monkeypatch, session_lifecycle_guard
):
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    service._jobs = {}
    service._thread_current_job = {}
    started_jobs: list[str] = []

    def fake_start_job_task(job):
        started_jobs.append(job.job_id)
        job.task = DummyTask(done=False)

    monkeypatch.setattr(service, "_start_job_task", fake_start_job_task)

    session_id = "session_queue_test"
    first_dispatch = await service.start_job(
        session_id,
        "first", thread_id=job_fixture_thread_id(session_id),
        message_id="msg_first",
        message_created_at="2026-07-14T00:00:00+00:00",
    )
    second_dispatch = await service.start_job(
        session_id,
        "second", thread_id=job_fixture_thread_id(session_id),
        message_id="msg_second",
        message_created_at="2026-07-14T00:00:01+00:00",
    )
    third_dispatch = await service.start_job(
        session_id,
        "third", thread_id=job_fixture_thread_id(session_id),
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
    assert service._thread_current_job[(session_id, job_fixture_thread_id(session_id))] == first_job_id
    assert list(service._pending_queue.queued_job_ids(session_id)) == [
        second_job_id,
        third_job_id,
    ]
    assert service._jobs[second_job_id].status == JobStatus.queued
    assert service._jobs[third_job_id].status == JobStatus.queued

    first_job = service._jobs[first_job_id]
    first_job.status = JobStatus.completed
    await service._schedule_next_job_if_needed(first_job)

    assert started_jobs == [first_job_id, second_job_id]
    assert service._thread_current_job[(session_id, job_fixture_thread_id(session_id))] == second_job_id
    assert list(service._pending_queue.queued_job_ids(session_id)) == [third_job_id]
    assert service._jobs[second_job_id].status == JobStatus.running


@pytest.mark.asyncio
async def test_boundary_notification_keeps_fifo_head_and_records_waiting_reason(job_service_factory,
    monkeypatch,
    session_lifecycle_guard,
):
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    monkeypatch.setattr(
        service,
        "_start_job_task",
        lambda job: setattr(job, "task", DummyTask(done=False)),
    )
    session_id = "session_boundary_dispatch"
    await service.start_job(
        session_id,
        "active", thread_id=job_fixture_thread_id(session_id),
        message_id="msg_active",
        message_created_at="2026-07-27T00:00:00+00:00",
    )
    queued = await service.start_job(
        session_id,
        "等待中断", thread_id=job_fixture_thread_id(session_id),
        message_id="msg_queued",
        message_created_at="2026-07-27T00:00:01+00:00",
        delivery_policy="after_interrupt",
    )

    snapshot = await service.notify_boundary(
        session_id,
        "after_tool_result", thread_id=job_fixture_thread_id(session_id),
        tool_result_available=True,
    )

    request = next(
        item for item in snapshot.requests if item.message_id == "msg_queued"
    )
    assert request.status == "queued"
    assert request.waiting_reason is None
    assert service._pending_queue.queued_job_ids(session_id) == (queued.job_id,)


@pytest.mark.asyncio
async def test_interrupt_releases_after_turn_head_instead_of_stalling_fifo(job_service_factory,
    monkeypatch,
    session_lifecycle_guard,
):
    """用户中断当前 turn 后，after_turn 队首必须被投递，不能永久停留在队列中。"""
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    started: list[str] = []

    def fake_start_job_task(job):
        started.append(job.job_id)
        job.task = DummyTask(done=False)

    monkeypatch.setattr(service, "_start_job_task", fake_start_job_task)
    session_id = "session_interrupt_releases_after_turn"
    active = await service.start_job(
        session_id,
        "active", thread_id=job_fixture_thread_id(session_id),
        message_id="msg_active",
        message_created_at="2026-07-28T00:00:00+00:00",
    )
    queued = await service.start_job(
        session_id,
        "queued", thread_id=job_fixture_thread_id(session_id),
        message_id="msg_queued",
        message_created_at="2026-07-28T00:00:01+00:00",
        delivery_policy="after_turn",
    )

    await service.control(
        active.job_id,
        JobControlRequest(action=ControlAction.cancel),
    )
    await service.notify_boundary(
        session_id,
        "after_interrupt", thread_id=job_fixture_thread_id(session_id),
        tool_result_available=False,
    )
    active_job = service._jobs[active.job_id]
    active_job.task = DummyTask(done=True)
    transition_job_status(
        active_job,
        JobStatus.cancelled,
        error_message="任务被用户取消",
    )
    await service._schedule_next_job_if_needed(active_job)

    assert started == [active.job_id, queued.job_id]
    assert service._jobs[queued.job_id].status == JobStatus.running
    assert service._thread_current_job[(session_id, job_fixture_thread_id(session_id))] == queued.job_id
    assert service._pending_queue.queued_job_ids(session_id) == ()


@pytest.mark.asyncio
async def test_direct_cancel_releases_after_turn_head_when_boundary_stays_idle(job_service_factory,
    monkeypatch,
    session_lifecycle_guard,
):
    """直接取消（未经过 notify_boundary、边界仍是 idle）也必须交还 Session。

    旧实现把 cancelled 的续跑条件锁定在 delivery_boundary == "after_interrupt"，
    只有中断边界路径才能放行 after_turn 队首；直接取消会让该会话的活动槽被腾空
    而队列队首永久滞留 pending。
    """
    service = create_job_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    started: list[str] = []

    def fake_start_job_task(job):
        started.append(job.job_id)
        job.task = DummyTask(done=False)

    monkeypatch.setattr(service, "_start_job_task", fake_start_job_task)
    session_id = "session_direct_cancel_idle_boundary"
    active = await service.start_job(
        session_id,
        "active", thread_id=job_fixture_thread_id(session_id),
        message_id="msg_active",
        message_created_at="2026-07-28T00:00:00+00:00",
    )
    queued = await service.start_job(
        session_id,
        "queued", thread_id=job_fixture_thread_id(session_id),
        message_id="msg_queued",
        message_created_at="2026-07-28T00:00:01+00:00",
        delivery_policy="after_turn",
    )

    await service.control(
        active.job_id,
        JobControlRequest(action=ControlAction.cancel),
    )
    # 刻意不调用 notify_boundary：活动槽的边界保持投递时的 "idle"。
    active_job = service._jobs[active.job_id]
    assert active_job.delivery_boundary == "idle"

    active_job.task = DummyTask(done=True)
    transition_job_status(
        active_job,
        JobStatus.cancelled,
        error_message="任务被用户取消",
    )
    await service._schedule_next_job_if_needed(active_job)

    assert started == [active.job_id, queued.job_id]
    assert service._jobs[queued.job_id].status == JobStatus.running
    assert service._thread_current_job[(session_id, job_fixture_thread_id(session_id))] == queued.job_id
    assert service._pending_queue.queued_job_ids(session_id) == ()
