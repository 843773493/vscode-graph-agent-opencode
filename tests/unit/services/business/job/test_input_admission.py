"""精确 Thread 准入、Task 所有权与删除排空。"""

from __future__ import annotations

import asyncio

import pytest

from app.services.business.job.input_admission import ThreadInputAdmission
from tests.unit.services.business.job.test_service_interrupt import create_job_service


@pytest.mark.asyncio
async def test_reentrant_owner_does_not_transfer_to_spawned_task() -> None:
    gate = ThreadInputAdmission()
    entered = asyncio.Event()

    async def contender() -> None:
        async with gate.hold("session", "thread"):
            entered.set()

    async with gate.hold("session", "thread"):
        async with gate.hold("session", "thread"):
            task = asyncio.create_task(contender())
            await asyncio.sleep(0)
            assert not entered.is_set()
        assert not entered.is_set()
    await task
    assert entered.is_set()
    assert not gate._entries


@pytest.mark.asyncio
async def test_waiter_cancellation_keeps_owner_and_allows_other_threads() -> None:
    gate = ThreadInputAdmission()
    entered = asyncio.Event()

    async def contender() -> None:
        async with gate.hold("ses_admission", "main"):
            entered.set()

    async with gate.hold("ses_admission", "main"):
        task = asyncio.create_task(contender())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with gate.hold("session", "child"):
            assert not entered.is_set()
        assert len(gate._entries) == 1
    await contender()
    assert entered.is_set()
    assert not gate._entries


@pytest.mark.asyncio
async def test_waiting_admission_counts_toward_delete_drain(
    job_service_factory, session_lifecycle_guard,
) -> None:
    service = create_job_service(
        session_lifecycle_guard, job_service_factory=job_service_factory,
    )
    waiting = asyncio.Event()

    async def contender() -> None:
        waiting.set()
        async with service.thread_input_admission("ses_admission", "main"):
            pytest.fail("等待准入应已取消")

    async with service.thread_input_admission("ses_admission", "main"):
        task = asyncio.create_task(contender())
        await waiting.wait()
        assert service._session_preparations == {"ses_admission": 2}
        drain = asyncio.create_task(service.drain_preparations(["ses_admission"]))
        await asyncio.sleep(0)
        assert not drain.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service._session_preparations == {"ses_admission": 1}
        assert not drain.done()
    await drain
    assert not service._session_preparations
    assert not service._thread_input_admission._entries


@pytest.mark.asyncio
async def test_finished_threads_do_not_accumulate_locks() -> None:
    gate = ThreadInputAdmission()
    for index in range(5000):
        async with gate.hold(f"ses_replay_{index}", f"thread_{index}"):
            assert len(gate._entries) == 1
    assert not gate._entries


@pytest.mark.asyncio
async def test_concurrent_inputs_on_same_thread_are_serialized() -> None:
    gate = ThreadInputAdmission()
    concurrent = 0
    peak = 0

    async def worker() -> None:
        nonlocal concurrent, peak
        async with gate.hold("session", "thread"):
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0)
            concurrent -= 1

    await asyncio.gather(*(worker() for _ in range(20)))
    assert peak == 1
    assert not gate._entries
