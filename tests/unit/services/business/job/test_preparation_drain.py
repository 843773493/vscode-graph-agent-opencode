"""准备计数归原调用所有，关闭等待不能提前结束或取消原调用。"""

from __future__ import annotations

import asyncio

import pytest

from app.services.business.job.service import JobService
from tests.unit.services.business.job.test_service_interrupt import create_job_service


@pytest.fixture
def preparation_jobs(job_service_factory, session_lifecycle_guard) -> JobService:
    return create_job_service(session_lifecycle_guard, job_service_factory=job_service_factory)


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_drain_waits_for_every_session_and_concurrent_window_without_cancelling_calls(
    preparation_jobs: JobService, outcome: str,
) -> None:
    jobs = preparation_jobs
    entered = asyncio.Queue[str]()
    release = {key: asyncio.Event() for key in ("a1", "a2", "b")}
    failure = OSError("原准备失败")

    async def prepare(key: str) -> str:
        entered.put_nowait(key)
        await release[key].wait()
        if key == "b" and outcome == "error":
            raise failure
        return key

    preparations = [asyncio.create_task(jobs.run_session_preparation(
        "ses_b" if key == "b" else "ses_a", lambda key=key: prepare(key),
    )) for key in release]
    try:
        assert {await entered.get() for _ in release} == set(release)
        assert jobs._session_preparations == {"ses_a": 2, "ses_b": 1}
        jobs.close_admission()
        abandoned = asyncio.create_task(jobs.drain_preparations())
        await asyncio.sleep(0)
        abandoned.cancel("取消本次排空等待")
        with pytest.raises(asyncio.CancelledError, match="取消本次排空等待"):
            await abandoned
        assert all(not task.done() for task in preparations)
        draining = asyncio.create_task(jobs.drain_preparations())
        for index, key in enumerate(("a1", "a2")):
            release[key].set()
            assert await preparations[index] == key
            await asyncio.sleep(0)
            assert not draining.done()
        assert jobs._session_preparations == {"ses_b": 1}
        if outcome == "cancel":
            preparations[-1].cancel("取消原准备")
            with pytest.raises(asyncio.CancelledError, match="取消原准备"):
                await preparations[-1]
        else:
            release["b"].set()
            if outcome == "error":
                with pytest.raises(OSError) as caught:
                    await preparations[-1]
                assert caught.value is failure
            else:
                assert await preparations[-1] == "b"
        await asyncio.wait_for(draining, 1)
        assert jobs._session_preparations == {}
    finally:
        for event in release.values():
            event.set()
        await asyncio.gather(*preparations, return_exceptions=True)
