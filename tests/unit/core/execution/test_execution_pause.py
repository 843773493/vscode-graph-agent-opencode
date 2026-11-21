from __future__ import annotations

import asyncio

import pytest

from app.core.execution_pause import ExecutionPause


@pytest.fixture
def pause() -> ExecutionPause:
    return ExecutionPause()


async def test_pause_waits_for_all_active_operations_and_blocks_new_ones(pause):
    started = [asyncio.Event(), asyncio.Event()]
    finish = [asyncio.Event(), asyncio.Event()]

    async def operation(index):
        async with pause.boundary():
            started[index].set()
            await finish[index].wait()

    tasks = [asyncio.create_task(operation(index)) for index in range(2)]
    await asyncio.gather(*(event.wait() for event in started))
    pause.request()
    assert pause.state == "pausing"
    finish[0].set()
    await tasks[0]
    assert pause.state == "pausing"
    finish[1].set()
    await tasks[1]
    assert pause.state == "paused"

    entered = asyncio.Event()

    async def next_operation():
        async with pause.boundary():
            entered.set()

    waiting = asyncio.create_task(next_operation())
    await asyncio.sleep(0)
    assert not entered.is_set()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert pause.active_count == 0
    pause.resume()
    await next_operation()
    assert entered.is_set()


async def test_clock_keeps_pausing_budget_and_excludes_only_static_pause(pause, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("app.core.execution_pause.monotonic", lambda: now[0])
    async with pause.boundary():
        pause.request()
        now[0] += 7
        assert pause.clock() == 107
    assert pause.state == "paused"
    now[0] += 50
    assert pause.clock() == 107
    pause.resume()
    now[0] += 3
    assert pause.clock() == 110


async def test_active_sleep_resumes_remaining_budget(pause):
    pause.request()
    timer = asyncio.create_task(pause.sleep_active(0.02))
    await asyncio.sleep(0.04)
    assert not timer.done()
    pause.resume()
    await timer
    assert pause.state == "running"
