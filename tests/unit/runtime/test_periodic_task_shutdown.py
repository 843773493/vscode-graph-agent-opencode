"""周期任务的原始失败不能在 Workspace 关闭时被普通取消吞掉。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.workspace_lifecycle import _report_periodic_failure, _stop_periodic_tasks


async def test_shutdown_cancels_all_tasks_before_waiting_and_preserves_errors() -> None:
    first = ConnectionError("原周期任务失败")
    second = OSError("取消收尾失败")
    cleaning, peer_cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def failed() -> None:
        raise first

    async def cleanup() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            raise second

    async def peer() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            peer_cancelled.set()

    tasks = [asyncio.create_task(work()) for work in (failed, cleanup, peer)]
    await asyncio.sleep(0)
    originals = tuple(tasks)
    closing = asyncio.create_task(_stop_periodic_tasks(tasks))
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        await asyncio.wait_for(peer_cancelled.wait(), 2)
        assert not closing.done() and tuple(tasks) == originals
        release.set()
        with pytest.raises(ExceptionGroup) as caught:
            await asyncio.wait_for(closing, 2)
        assert caught.value.exceptions == (first, second)
        assert tasks == [] and all(task.done() for task in originals)
        # 失败已如实交回关闭调用方，重试不重放已结算的周期工作。
        await _stop_periodic_tasks(tasks)
    finally:
        release.set()
        await asyncio.gather(closing, *originals, return_exceptions=True)


async def test_normal_completion_and_shutdown_cancellation_are_not_failures() -> None:
    async def completed() -> None:
        return

    async def waiting() -> None:
        await asyncio.Event().wait()

    tasks = [asyncio.create_task(completed()), asyncio.create_task(waiting())]
    await asyncio.sleep(0)
    originals = tuple(tasks)
    await _stop_periodic_tasks(tasks)
    assert tasks == []
    assert originals[0].result() is None and originals[1].cancelled()


async def test_periodic_failure_is_logged_with_original_traceback(caplog: pytest.LogCaptureFixture) -> None:
    failure = RuntimeError("原周期回收异常")

    async def failed() -> None:
        raise failure

    with caplog.at_level(logging.ERROR, logger="app.workspace_lifecycle"):
        task = asyncio.create_task(failed(), name="activity-prune:exact-workspace")
        task.add_done_callback(_report_periodic_failure)
        await asyncio.gather(task, return_exceptions=True)
    record, = caplog.records
    assert "activity-prune:exact-workspace" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[1] is failure
    assert record.exc_info[2] is failure.__traceback__


async def test_normal_periodic_stop_does_not_log_an_error(caplog: pytest.LogCaptureFixture) -> None:
    task = asyncio.create_task(asyncio.sleep(3600))
    task.add_done_callback(_report_periodic_failure)
    await _stop_periodic_tasks([task])
    assert caplog.records == []
