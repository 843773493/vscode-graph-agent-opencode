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


class _InstantExecutor:
    def __init__(self) -> None:
        self.ran = 0

    async def run(self, job):
        del job
        self.ran += 1
        return "done"


async def _wait_slot_cleared(service: JobService, session_id: str) -> None:
    for _ in range(200):
        if service._session_current_job.get(session_id) is None:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"会话活动槽未释放: session_id={session_id}")


def _make_service(session_lifecycle_guard) -> tuple[JobService, _InstantExecutor]:
    executor = _InstantExecutor()
    return JobService(
        job_event_bus=JobEventBus(),
        job_executor=executor,
        session_lifecycle_guard=session_lifecycle_guard,
    ), executor


@pytest.mark.asyncio
async def test_cancel_before_task_body_runs_converges_to_cancelled(
    session_lifecycle_guard,
) -> None:
    service, executor = _make_service(session_lifecycle_guard=session_lifecycle_guard)
    session_id = "session_cancel_before_start"
    dispatch = await service.start_job(
        session_id,
        "第一条",
        message_id="msg_first",
        message_created_at=datetime.now(UTC).isoformat(),
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
async def test_cancel_before_task_body_releases_fifo_head(
    session_lifecycle_guard,
) -> None:
    service, executor = _make_service(session_lifecycle_guard=session_lifecycle_guard)
    session_id = "session_cancel_before_start_fifo"
    first = await service.start_job(
        session_id,
        "第一条",
        message_id="msg_first",
        message_created_at=datetime.now(UTC).isoformat(),
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
    )
    for _ in range(200):
        if service._jobs[second.job_id].status == JobStatus.completed:
            break
        await asyncio.sleep(0.005)

    assert service._jobs[second.job_id].status == JobStatus.completed
    assert executor.ran == 1


@pytest.mark.asyncio
async def test_concurrent_cancel_before_start_never_stalls_in_cancelling(
    session_lifecycle_guard,
) -> None:
    """100 次并发「派发后立即取消」全部收敛，不允许残留 cancelling。"""

    for index in range(100):
        await _run_concurrent_cancel_case(
            index,
            session_lifecycle_guard=session_lifecycle_guard,
        )


async def _run_concurrent_cancel_case(index: int, session_lifecycle_guard) -> None:
    service, executor = _make_service(session_lifecycle_guard=session_lifecycle_guard)
    session_id = f"session_cancel_race_{index}"
    started = asyncio.Event()
    holder: list[str] = []

    async def stopper() -> None:
        await started.wait()
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
async def test_pause_before_task_body_runs_resumes_cleanly(
    session_lifecycle_guard,
) -> None:
    """同一未启动任务的 pause 变体：任务已死但语义未终结，resume 后须继续。"""

    service, executor = _make_service(session_lifecycle_guard=session_lifecycle_guard)
    session_id = "session_pause_before_start"
    dispatch = await service.start_job(
        session_id,
        "第一条",
        message_id="msg_pause",
        message_created_at=datetime.now(UTC).isoformat(),
    )
    job = service._jobs[dispatch.job_id]

    await service.control(
        dispatch.job_id,
        JobControlRequest(action=ControlAction.pause),
    )
    await asyncio.sleep(0.02)
    assert job.status == JobStatus.paused

    result = await service.control(
        dispatch.job_id,
        JobControlRequest(action=ControlAction.resume),
    )
    assert result.status == JobStatus.running
    await _wait_slot_cleared(service, session_id)

    assert job.status == JobStatus.completed
    assert executor.ran == 1


@pytest.mark.asyncio
async def test_cancel_after_task_body_started_keeps_task_path_terminal(
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
    service = JobService(
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


async def _wait_job_task_done(service: JobService, job_id: str) -> None:
    for _ in range(200):
        task = service._jobs[job_id].task
        if task is not None and task.done():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"Job 执行任务未结束: job_id={job_id}")


class _BlockFirstThenFast:
    """第一条阻塞（模拟长任务），后续消息立即完成。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.first_job_id: str | None = None

    async def run(self, job):
        if self.first_job_id is None:
            self.first_job_id = job.job_id
        if job.job_id == self.first_job_id:
            self.started.set()
            await asyncio.Future()
        return "ok"


@pytest.mark.asyncio
async def test_cancel_paused_after_task_done_releases_slot_and_wakes_fifo(
    session_lifecycle_guard,
) -> None:
    """P-1 回归：暂停后等执行任务彻底结束再取消，槽必须释放且 FIFO 队首被唤醒。

    暂停时任务体的 finally 已跑完并调用过调度（当时是 paused，被早退跳过），
    之后再取消一个已 done 的任务不会重跑 finally。因此「取消 paused」必须自己
    在写入终态的同一条链路上释放活动槽，否则会话 FIFO 队首永久停在 queued。
    """

    executor = _BlockFirstThenFast()
    service = JobService(
        job_event_bus=JobEventBus(),
        job_executor=executor,
        session_lifecycle_guard=session_lifecycle_guard,
    )
    session_id = "session_paused_cancel_fifo"
    first = await service.start_job(
        session_id,
        "第一条",
        message_id="msg_first",
        message_created_at=datetime.now(UTC).isoformat(),
    )
    await executor.started.wait()
    second = await service.start_job(
        session_id,
        "第二条",
        message_id="msg_second",
        message_created_at=datetime.now(UTC).isoformat(),
    )
    assert service._jobs[second.job_id].status == JobStatus.queued

    await service.control(
        first.job_id,
        JobControlRequest(action=ControlAction.pause),
    )
    await _wait_job_task_done(service, first.job_id)

    await service.control(
        first.job_id,
        JobControlRequest(action=ControlAction.cancel),
    )

    assert service._jobs[first.job_id].status == JobStatus.cancelled
    # paused 被取消后活动槽必须立刻让位：要么空队列时清空，要么直接交给
    # 下一条 FIFO 队首；绝不允许仍停留在已终态的 first.job_id。
    assert service._session_current_job.get(session_id) != first.job_id, (
        "取消 paused Job 后活动槽仍停留在已终态 Job（终态与槽释放未在同一链路）"
    )

    for _ in range(200):
        if service._jobs[second.job_id].status == JobStatus.completed:
            break
        await asyncio.sleep(0.005)
    assert service._jobs[second.job_id].status == JobStatus.completed, (
        f"FIFO 队首未被唤醒: {service._jobs[second.job_id].status.value}"
    )
    await _wait_slot_cleared(service, session_id)


@pytest.mark.asyncio
async def test_force_interrupt_paused_releases_slot(session_lifecycle_guard) -> None:
    """P2 变体：运行期排空作用于 paused Job 时也必须释放活动槽。"""

    executor = _BlockFirstThenFast()
    service = JobService(
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
    )
    await executor.started.wait()

    await service.control(
        first.job_id,
        JobControlRequest(action=ControlAction.pause),
    )
    await _wait_job_task_done(service, first.job_id)

    drained = await service.force_interrupt_active(reason="runtime_restart")

    assert drained == 1
    assert service._jobs[first.job_id].status == JobStatus.cancelled
    assert service._session_current_job.get(session_id) is None, (
        "排空 paused Job 后活动槽必须已释放"
    )
