"""_touch_active_job 心跳循环的状态边界。

心跳在「活跃 + 取消进行中」期间必须持续推进 progress/current_step/updated_at，
直到终态写入点接管；进入非终态的静止态（paused）或终态则停止。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import pytest

from app.schemas.internal_v2.common import JobStatus
from app.services.business.job.runtime_state import JobRuntimeState
from app.services.business.job.service import JobService, JobState


def _make_job(status: JobStatus) -> JobState:
    return JobState(
        job_id="job_heartbeat",
        session_id="ses_heartbeat",
        message="心跳",
        message_id="msg_heartbeat",
        message_created_at="2026-10-01T00:00:00+00:00",
        agent_id="default",
        status=status,
    )


def _make_runtime_state(progress: int, current_step: str) -> JobRuntimeState:
    return JobRuntimeState(
        job_id="job_heartbeat",
        session_id="ses_heartbeat",
        message="心跳",
        agent_id="default",
        message_id="msg_heartbeat",
        message_created_at="2026-10-01T00:00:00+00:00",
        progress=progress,
        current_step=current_step,
    )

async def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_heartbeat_keeps_advancing_while_cancelling() -> None:
    """cancelling 属于心跳追踪集合：取消进行中仍须推进可观察进度。"""

    job = _make_job(JobStatus.cancelling)
    runtime_state = _make_runtime_state(progress=7, current_step="agent_tool")
    before = job.updated_at

    heartbeat = asyncio.create_task(JobService._touch_active_job(job, runtime_state))
    advanced = await _wait_until(
        lambda: (
            job.progress == 7
            and job.current_step == "agent_tool"
            and job.updated_at > before
        )
    )
    # 由终态写入点接管后，心跳循环必须优雅退出。
    job.status = JobStatus.cancelled
    await asyncio.wait_for(heartbeat, timeout=5)

    assert advanced, "cancelling 期间心跳必须推进 progress/current_step/updated_at"


@pytest.mark.asyncio
async def test_heartbeat_stops_on_non_tracked_status() -> None:
    """paused 不在心跳追踪集合内：循环须立即停止，不推进任何字段。"""

    job = _make_job(JobStatus.paused)
    runtime_state = _make_runtime_state(progress=7, current_step="agent_tool")
    before = job.updated_at

    heartbeat = JobService._touch_active_job(job, runtime_state)
    await asyncio.wait_for(heartbeat, timeout=3)

    assert job.progress == 0
    assert job.current_step is None
    assert job.updated_at == before


def test_heartbeat_tracked_statuses_cover_cancelling() -> None:
    """常量必须覆盖活跃四态并额外并入 cancelling，且不含终态/静止态。"""

    from app.services.business.job.lifecycle import (
        ACTIVE_JOB_STATUSES,
        HEARTBEAT_TRACKED_JOB_STATUSES,
    )

    assert HEARTBEAT_TRACKED_JOB_STATUSES == ACTIVE_JOB_STATUSES | {
        JobStatus.cancelling
    }
    assert JobStatus.paused not in HEARTBEAT_TRACKED_JOB_STATUSES
    assert JobStatus.completed not in HEARTBEAT_TRACKED_JOB_STATUSES
