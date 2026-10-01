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


def _make_service() -> tuple[JobService, _InstantExecutor]:
    executor = _InstantExecutor()
    return JobService(job_event_bus=JobEventBus(), job_executor=executor), executor


@pytest.mark.asyncio
async def test_cancel_before_task_body_runs_converges_to_cancelled() -> None:
    service, executor = _make_service()
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
async def test_cancel_before_task_body_releases_fifo_head() -> None:
    service, executor = _make_service()
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
async def test_concurrent_cancel_before_start_never_stalls_in_cancelling() -> None:
    """100 次并发「派发后立即取消」全部收敛，不允许残留 cancelling。"""

    for index in range(100):
        await _run_concurrent_cancel_case(index)


async def _run_concurrent_cancel_case(index: int) -> None:
    service, executor = _make_service()
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
async def test_pause_before_task_body_runs_resumes_cleanly() -> None:
    """同一未启动任务的 pause 变体：任务已死但语义未终结，resume 后须继续。"""

    service, executor = _make_service()
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
async def test_cancel_after_task_body_started_keeps_task_path_terminal() -> None:
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
    service = JobService(job_event_bus=JobEventBus(), job_executor=executor)
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
