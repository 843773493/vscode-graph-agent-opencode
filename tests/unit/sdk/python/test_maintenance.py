from __future__ import annotations

import asyncio

import pytest
from boxteam_plugin_sdk.lifecycle import LifetimeScope
from boxteam_plugin_sdk.maintenance import MaintenanceTask, start_maintenance


@pytest.mark.asyncio
async def test_invalid_static_set_creates_no_maintenance_tasks() -> None:
    scope = LifetimeScope("invalid-maintenance")

    with pytest.raises(ValueError, match="超出支持范围"):
        start_maintenance(
            [
                MaintenanceTask("valid", 1, False, _wait),
                MaintenanceTask("invalid", 2_147_483_648, False, _wait),
            ],
            scope=scope,
            identity="workspace-a",
            report_error=lambda *_: None,
        )

    assert scope.snapshot().task_count == 0
    await scope.close()


@pytest.mark.asyncio
async def test_failure_is_reported_and_next_cycle_runs_independently() -> None:
    scope = LifetimeScope("maintenance-retry")
    second_run = asyncio.Event()
    calls = 0
    reports: list[tuple[str, object, float, Exception]] = []

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("maintenance failure")
        second_run.set()

    start_maintenance(
        [MaintenanceTask("flaky", 5, True, flaky)],
        scope=scope,
        identity="workspace-a",
        report_error=lambda *failure: reports.append(failure),
    )
    await asyncio.wait_for(second_run.wait(), timeout=1)
    await scope.close()

    assert calls == 2
    assert len(reports) == 1
    assert reports[0][0:2] == ("flaky", "workspace-a")
    assert reports[0][2] >= 0
    assert isinstance(reports[0][3], OSError)


@pytest.mark.asyncio
async def test_repeated_assembly_on_same_scope_is_rejected() -> None:
    scope = LifetimeScope("duplicate-maintenance")
    task = MaintenanceTask("one", 1000, False, _wait)
    start_maintenance([task], scope=scope, identity="workspace-a", report_error=lambda *_: None)
    with pytest.raises(RuntimeError, match="重复装配"):
        start_maintenance([task], scope=scope, identity="workspace-a", report_error=lambda *_: None)
    await scope.close()


@pytest.mark.asyncio
async def test_shutdown_error_is_returned_by_scope_close_not_reported() -> None:
    scope = LifetimeScope("maintenance-shutdown-error")
    entered = asyncio.Event()
    finish = asyncio.Event()
    reports: list[tuple[str, object, float, Exception]] = []
    failure = OSError("shutdown I/O failed")

    async def cleanup() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await finish.wait()
            raise failure

    worker, = start_maintenance(
        [MaintenanceTask("cleanup", 5, True, cleanup)],
        scope=scope,
        identity="workspace-a",
        report_error=lambda *error: reports.append(error),
    )
    await entered.wait()
    closing = asyncio.create_task(scope.close())
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(ExceptionGroup) as captured:
        await closing

    assert captured.value.exceptions[0].__cause__ is failure
    assert worker.exception() is failure
    assert reports == []
    assert scope.snapshot().task_count == 0


@pytest.mark.asyncio
async def test_shutdown_that_suppresses_cancel_does_not_start_another_cycle(monkeypatch) -> None:
    scope = LifetimeScope("maintenance-shutdown-return")
    entered = asyncio.Event()
    cancelling = asyncio.Event()
    finish = asyncio.Event()
    original_sleep = asyncio.sleep
    maintenance_sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        if seconds == 0.005:
            maintenance_sleeps.append(seconds)
        await original_sleep(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def cleanup() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await finish.wait()

    start_maintenance(
        [MaintenanceTask("cleanup", 5, True, cleanup)],
        scope=scope,
        identity="workspace-a",
        report_error=lambda *_: None,
    )
    await entered.wait()
    closing = asyncio.create_task(scope.close())
    await cancelling.wait()
    finish.set()
    await closing

    assert maintenance_sleeps == []
    assert scope.is_closed


@pytest.mark.asyncio
async def test_slow_task_does_not_overlap_or_block_an_independent_task(monkeypatch) -> None:
    scope = LifetimeScope("maintenance-independent-cycles")
    slow_entered = asyncio.Event()
    release_slow = asyncio.Event()
    slow_wait = asyncio.Event()
    fast_wait = asyncio.Event()
    fast_second_run = asyncio.Event()
    slow_second_run = asyncio.Event()
    slow_calls = 0
    fast_calls = 0
    active_slow_calls = 0
    max_active_slow_calls = 0
    original_sleep = asyncio.sleep
    slow_task: asyncio.Future[object] | None = None
    fast_task: asyncio.Future[object] | None = None

    async def controlled_sleep(seconds: float) -> None:
        current = asyncio.current_task()
        if seconds == 0.005 and current is slow_task:
            await slow_wait.wait()
            slow_wait.clear()
            return
        if seconds == 0.005 and current is fast_task:
            await fast_wait.wait()
            fast_wait.clear()
            return
        await original_sleep(seconds)

    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)

    async def slow() -> None:
        nonlocal slow_calls, active_slow_calls, max_active_slow_calls
        slow_calls += 1
        active_slow_calls += 1
        max_active_slow_calls = max(max_active_slow_calls, active_slow_calls)
        if slow_calls == 1:
            slow_entered.set()
            await release_slow.wait()
        else:
            slow_second_run.set()
        active_slow_calls -= 1

    async def fast() -> None:
        nonlocal fast_calls
        fast_calls += 1
        if fast_calls == 2:
            fast_second_run.set()

    slow_task, fast_task = start_maintenance(
        [MaintenanceTask("slow", 5, True, slow), MaintenanceTask("fast", 5, True, fast)],
        scope=scope,
        identity="workspace-a",
        report_error=lambda *_: None,
    )
    try:
        await asyncio.wait_for(slow_entered.wait(), 1)
        fast_wait.set()
        await asyncio.wait_for(fast_second_run.wait(), 1)
        assert slow_calls == 1 and max_active_slow_calls == 1
        release_slow.set()
        slow_wait.set()
        await asyncio.wait_for(slow_second_run.wait(), 1)
        assert slow_calls == 2 and max_active_slow_calls == 1
    finally:
        release_slow.set()
        await scope.close()


async def _wait() -> None:
    await asyncio.Event().wait()
