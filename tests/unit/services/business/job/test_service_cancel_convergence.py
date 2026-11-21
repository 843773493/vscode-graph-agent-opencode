"""P1-A 回归：任务体尚未被调度即被取消时必须收敛到 cancelled。

用户取消或运行期排空会先把 Job 写成 ``cancelling`` 再 ``task.cancel()``。
若协程从未进入 ``_run_job_background`` 的 try，``CancelledError`` 直接在
task 层面终结，权威终态写入点没有机会执行，Job 会永久停在非终态的
``cancelling``，会话活动槽与 FIFO 队首一并卡死。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.core.job_event_bus import JobEventBus
from app.schemas.internal_v2.common import ControlAction, JobStatus
from app.schemas.internal_v2.job import JobControlRequest
from app.services.business.job.service import JobService
from tests.support.job_thread_fixture import job_fixture_thread_id


class _InstantExecutor:
    def __init__(self) -> None:
        self.ran = 0

    async def run(self, job):
        async with job.execution_pause.boundary():
            self.ran += 1
            return "done"


async def _wait_slot_cleared(service: JobService, session_id: str) -> None:
    for _ in range(200):
        if service._thread_current_job.get((session_id, job_fixture_thread_id(session_id))) is None:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"会话活动槽未释放: session_id={session_id}")


def _make_service(session_lifecycle_guard, *, job_service_factory) -> tuple[JobService, _InstantExecutor]:
    executor = _InstantExecutor()
    return job_service_factory(
        job_event_bus=JobEventBus(),
        job_executor=executor,
        session_lifecycle_guard=session_lifecycle_guard,
    ), executor


def _hold_first_task_before_body(service: JobService, monkeypatch: pytest.MonkeyPatch) -> None:
    """准入本身可以让出事件循环；显式固定首次任务体尚未进入的取消窗口。"""
    original = service._run_job_background
    first = True

    async def run(*args):
        nonlocal first
        if first:
            first = False
            await asyncio.Future()
        return await original(*args)

    monkeypatch.setattr(service, "_run_job_background", run)


@pytest.mark.asyncio
async def test_cancel_before_task_body_runs_converges_to_cancelled(job_service_factory,
    session_lifecycle_guard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, executor = _make_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    _hold_first_task_before_body(service, monkeypatch)
    session_id = "session_cancel_before_start"
    dispatch = await service.start_job(
        session_id,
        "第一条",
        message_id="msg_first",
        message_created_at=datetime.now(UTC).isoformat(),
        thread_id=job_fixture_thread_id(session_id),
    )
    job = service._jobs[dispatch.job_id]

    await service.control(
        dispatch.job_id,
        JobControlRequest(action=ControlAction.cancel),
    )
    await _wait_slot_cleared(service, session_id)

    assert job.status == JobStatus.cancelled
    assert job.ended_at is not None
    assert executor.ran == 0, "任务体不应被执行"


@pytest.mark.asyncio
async def test_cancel_before_task_body_releases_fifo_head(job_service_factory,
    session_lifecycle_guard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, executor = _make_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    _hold_first_task_before_body(service, monkeypatch)
    session_id = "session_cancel_before_start_fifo"
    first = await service.start_job(
        session_id,
        "第一条",
        message_id="msg_first",
        message_created_at=datetime.now(UTC).isoformat(),
        thread_id=job_fixture_thread_id(session_id),
    )
    await service.control(
        first.job_id,
        JobControlRequest(action=ControlAction.cancel),
    )

    second = await service.start_job(
        session_id,
        "第二条",
        message_id="msg_second",
        message_created_at=datetime.now(UTC).isoformat(),
        thread_id=job_fixture_thread_id(session_id),
    )
    for _ in range(200):
        if service._jobs[second.job_id].status == JobStatus.completed:
            break
        await asyncio.sleep(0.005)

    assert service._jobs[second.job_id].status == JobStatus.completed
    assert executor.ran == 1


@pytest.mark.asyncio
async def test_concurrent_cancel_before_start_never_stalls_in_cancelling(job_service_factory,
    session_lifecycle_guard,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """100 次并发「派发后立即取消」全部收敛，不允许残留 cancelling。"""

    for index in range(100):
        await _run_concurrent_cancel_case(
            index,
            session_lifecycle_guard=session_lifecycle_guard,
            monkeypatch=monkeypatch,
        job_service_factory=job_service_factory)


async def _run_concurrent_cancel_case(index: int, session_lifecycle_guard, monkeypatch: pytest.MonkeyPatch, *, job_service_factory) -> None:
    service, executor = _make_service(session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory)
    # 准入恢复可以让出事件循环；显式阻挡任务体，保证测到启动前取消而非执行中取消。
    entered = asyncio.Event()

    async def wait_before_task_body(*_args) -> None:
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(service, "_run_job_background", wait_before_task_body)
    session_id = f"session_cancel_race_{index}"
    started = asyncio.Event()
    holder: list[str] = []

    async def stopper() -> None:
        await started.wait()
        await entered.wait()
        await service.control(
            holder[0],
            JobControlRequest(action=ControlAction.cancel),
        )

    stopper_task = asyncio.create_task(stopper())
    dispatch = await service.start_job(
        session_id,
        "消息",
        message_id=f"msg_{index}",
        message_created_at=datetime.now(UTC).isoformat(),
        thread_id=job_fixture_thread_id(session_id),
    )
    holder.append(dispatch.job_id)
    started.set()
    await stopper_task
    await _wait_slot_cleared(service, session_id)

    job = service._jobs[dispatch.job_id]
    assert job.status == JobStatus.cancelled, (
        f"第 {index} 次残留非终态: {job.status.value}"
    )
    assert executor.ran == 0


@pytest.mark.asyncio
async def test_pause_before_task_body_preserves_original_task(
    job_service_factory, session_lifecycle_guard, monkeypatch,
) -> None:
    service, executor = _make_service(
        session_lifecycle_guard=session_lifecycle_guard, job_service_factory=job_service_factory,
    )
    original = service._run_job_background
    release_body = asyncio.Event()

    async def held_body(*args):
        await release_body.wait()
        return await original(*args)

    monkeypatch.setattr(service, "_run_job_background", held_body)
    session_id = "session_pause_before_start"
    dispatch = await service.start_job(
        session_id, "第一条", message_id="msg_pause",
        message_created_at=datetime.now(UTC).isoformat(),
        thread_id=job_fixture_thread_id(session_id),
    )
    job = service._jobs[dispatch.job_id]
    original_task = job.task
    try:
        response = await service.control(job.job_id, JobControlRequest(action=ControlAction.pause))
        release_body.set()
        await asyncio.sleep(0.02)
        assert response.control_state == "paused"
        assert job.status == JobStatus.running
        assert job.task is original_task and not original_task.done()
        assert executor.ran == 0
        await service.control(job.job_id, JobControlRequest(action=ControlAction.resume))
        await original_task
        assert job.status == JobStatus.completed
        assert executor.ran == 1
        assert job.task is original_task
    finally:
        release_body.set()
        if not original_task.done():
            await service.control(job.job_id, JobControlRequest(action=ControlAction.cancel))
            await asyncio.gather(original_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_after_task_body_started_keeps_task_path_terminal(job_service_factory,
    session_lifecycle_guard,
) -> None:
    """任务体已进入执行时，权威写入点仍是唯一终态写入者。"""

    entered = asyncio.Event()

    class _BlockingExecutor:
        def __init__(self) -> None:
            self.ran = 0

        async def run(self, job):
            del job
            self.ran += 1
            entered.set()
            await asyncio.Future()

    executor = _BlockingExecutor()
    service = job_service_factory(
        job_event_bus=JobEventBus(),
        job_executor=executor,
        session_lifecycle_guard=session_lifecycle_guard,
    )
    session_id = "session_cancel_after_start"
    dispatch = await service.start_job(
        session_id,
        "消息",
        message_id="msg_running",
        message_created_at=datetime.now(UTC).isoformat(),
        thread_id=job_fixture_thread_id(session_id),
    )
    job = service._jobs[dispatch.job_id]
    await entered.wait()

    await service.control(
        dispatch.job_id,
        JobControlRequest(action=ControlAction.cancel),
    )
    await _wait_slot_cleared(service, session_id)

    assert job.status == JobStatus.cancelled
    assert executor.ran == 1


@pytest.mark.asyncio
async def test_cancel_paused_drains_terminal_owner_and_keeps_queued_job_paused(
    job_service_factory, session_lifecycle_guard, boundary_executor, terminal_writer,
) -> None:
    service = job_service_factory(
        job_event_bus=JobEventBus(), job_executor=boundary_executor,
        session_lifecycle_guard=session_lifecycle_guard, terminal_status_writer=terminal_writer,
    )
    session_id = "session_paused_cancel_fifo"
    thread_id = job_fixture_thread_id(session_id)
    first = await service.start_job(
        session_id, "第一条", message_id="msg_first",
        message_created_at=datetime.now(UTC).isoformat(), thread_id=thread_id,
    )
    job = service._jobs[first.job_id]
    queue = await service._bus.subscribe(first.job_id, subscriber_kind="test_cancel")
    await boundary_executor.started.wait()
    original_task, original_execution = job.task, job.execution_task
    second = await service.start_job(
        session_id, "第二条", message_id="msg_second",
        message_created_at=datetime.now(UTC).isoformat(), thread_id=thread_id,
    )
    queued = service._jobs[second.job_id]
    try:
        await service.control(first.job_id, JobControlRequest(action=ControlAction.pause))
        boundary_executor.finish_first.set()
        await boundary_executor.first_finished.wait()
        assert job.execution_pause.state == "paused"
        assert not original_task.done() and not original_execution.done()
        assert not await service._start_next_pending(session_id, thread_id=thread_id)
        assert queued.status == JobStatus.queued and queued.task is None
        terminal_writer.mark_job_terminal_status.assert_not_called()

        await service.control(first.job_id, JobControlRequest(action=ControlAction.cancel))
        await service.settle_jobs([first.job_id])
        assert job.status == JobStatus.cancelled
        assert original_task.done() and original_execution.done()
        terminal_writer.mark_job_terminal_status.assert_called_once_with(
            session_id=session_id, thread_id=thread_id, job_id=first.job_id, status="cancelled",
        )
        assert queued.status == JobStatus.queued and queued.task is None
        snapshot = await service.thread_control_snapshot(session_id, thread_id)
        assert snapshot.state == "paused" and snapshot.execution_id is None
        events = [queue.get_nowait() for _ in range(queue.qsize())]
        assert [event.type for event in events].count("job_cancelled") == 1

        await service.control_thread_advancement(
            session_id, thread_id, action="resume", expected_execution_id=None,
        )
        await queued.task
        assert queued.status == JobStatus.completed
        await _wait_slot_cleared(service, session_id)
        assert [runtime.job_id for runtime in boundary_executor.runtimes] == [first.job_id, second.job_id]
    finally:
        if not original_task.done():
            await service.control(first.job_id, JobControlRequest(action=ControlAction.cancel))
            await service.settle_jobs([first.job_id])
        if queued.task is not None:
            await queued.task


@pytest.mark.asyncio
@pytest.mark.usefixtures("empty_workspace_scope")
async def test_force_interrupt_paused_releases_slot(job_service_factory, session_lifecycle_guard, boundary_executor) -> None:
    """P2 变体：运行期排空作用于 paused Job 时也必须释放活动槽。"""

    executor = boundary_executor
    service = job_service_factory(
        job_event_bus=JobEventBus(),
        job_executor=executor,
        session_lifecycle_guard=session_lifecycle_guard,
    )
    session_id = "session_force_interrupt_paused"
    first = await service.start_job(
        session_id,
        "第一条",
        message_id="msg_first",
        message_created_at=datetime.now(UTC).isoformat(),
        thread_id=job_fixture_thread_id(session_id),
    )
    await executor.started.wait()

    await service.control(
        first.job_id,
        JobControlRequest(action=ControlAction.pause),
    )
    executor.finish_first.set()
    await executor.first_finished.wait()
    assert service._jobs[first.job_id].execution_pause.state == "paused"

    drained = await service.force_interrupt_active(reason="runtime_restart")

    assert drained == 1
    assert service._jobs[first.job_id].status == JobStatus.cancelled
    assert service._thread_current_job.get((session_id, job_fixture_thread_id(session_id))) is None, (
        "排空 paused Job 后活动槽必须已释放"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("task_finished", [False, True])
async def test_cancel_active_without_live_task_releases_slot(job_service_factory,
    session_lifecycle_guard, monkeypatch: pytest.MonkeyPatch, task_finished: bool,
) -> None:
    service, executor = _make_service(session_lifecycle_guard, job_service_factory=job_service_factory)
    session_id = "session_taskless_cancel"
    identity = service.reserve_user_execution_identity()

    async def finished() -> None:
        return

    def interrupted_start(job) -> None:
        if task_finished:
            job.task = asyncio.create_task(finished())
            return
        raise OSError("injected task start failure")

    monkeypatch.setattr(service, "_start_job_task", interrupted_start)

    async def start() -> None:
        await service.start_job(
            session_id, "第一条", message_id="msg_taskless",
            message_created_at=datetime.now(UTC).isoformat(),
            thread_id=job_fixture_thread_id(session_id), execution_binding=identity,
        )

    if task_finished:
        await start()
        await service._jobs[identity.job_id].task
    else:
        with pytest.raises(OSError, match="injected task start"):
            await start()
    queue = await service._bus.subscribe(identity.job_id, subscriber_kind="test_cancel")
    result = await service.control(identity.job_id, JobControlRequest(action=ControlAction.cancel))
    assert result.status == service._jobs[identity.job_id].status == JobStatus.cancelled
    assert service._jobs[identity.job_id].ended_at is not None
    assert (session_id, job_fixture_thread_id(session_id)) not in service._thread_current_job
    assert executor.ran == 0
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [event.type for event in events].count("job_cancelled") == 1
