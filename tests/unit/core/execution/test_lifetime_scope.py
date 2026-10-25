from __future__ import annotations

import asyncio
import inspect

import pytest

from app.core.lifecycle import LifetimeScope


@pytest.mark.asyncio
async def test_lifetime_scope_releases_children_then_resources_in_reverse_order() -> None:
    calls: list[str] = []
    scope = LifetimeScope("turn")
    scope.register(lambda: calls.append("parent-first"), label="parent-first")
    child = scope.child("model-call")
    child.register(lambda: calls.append("child-first"), label="child-first")
    scope.register(lambda: calls.append("parent-last"), label="parent-last")

    await scope.close()

    assert calls == ["child-first", "parent-last", "parent-first"]
    assert scope.snapshot().state == "closed"


@pytest.mark.asyncio
async def test_lifetime_scope_rejects_registration_while_closing() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    scope = LifetimeScope("blocked")

    async def close_resource() -> None:
        entered.set()
        await release.wait()

    scope.register(close_resource, label="blocked-resource")
    closing = asyncio.create_task(scope.close())
    await entered.wait()

    with pytest.raises(RuntimeError, match="不允许注册"):
        scope.register(lambda: None)

    release.set()
    await closing


@pytest.mark.asyncio
async def test_lifetime_scope_concurrent_close_is_idempotent() -> None:
    calls = 0

    async def close_resource() -> None:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)

    scope = LifetimeScope("concurrent")
    scope.register(close_resource)
    await asyncio.gather(scope.close(), scope.close(), scope.close())

    assert calls == 1
    assert scope.is_closed


@pytest.mark.asyncio
async def test_concurrent_revoke_and_close_share_the_owned_release() -> None:
    scope = LifetimeScope("release-race")
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def cleanup() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()

    handle = scope.register(cleanup)
    first = asyncio.create_task(handle.revoke())
    second = asyncio.create_task(handle.revoke())
    await entered.wait()
    closing = asyncio.create_task(scope.close())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()
    await asyncio.gather(second, closing)

    assert calls == 1
    assert handle.revoked and scope.is_closed


@pytest.mark.asyncio
async def test_close_drains_an_awaitable_before_its_first_step() -> None:
    scope = LifetimeScope("unstarted")

    async def operation() -> None:
        await asyncio.Event().wait()

    pending = operation()
    task = scope.start_task(lambda: pending, label="unstarted")
    task.cancel()
    await scope.close()

    assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED
    assert scope.snapshot().task_count == 0


@pytest.mark.asyncio
async def test_lifetime_scope_returns_failure_and_retries_failed_resource() -> None:
    calls = 0

    def flaky_close() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("still attached")

    scope = LifetimeScope("retry")
    scope.register(flaky_close, label="external")

    with pytest.raises(ExceptionGroup, match="close 失败"):
        await scope.close()
    assert scope.snapshot().state == "close_failed"

    await scope.close()
    assert calls == 2
    assert scope.snapshot().state == "closed"


@pytest.mark.asyncio
async def test_concurrent_close_waiters_receive_the_same_failure() -> None:
    scope = LifetimeScope("shared-failure")
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = 0
    failure = OSError("release failure")

    async def cleanup() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        raise failure

    scope.register(cleanup)
    waiters = [asyncio.create_task(scope.close()) for _ in range(3)]
    await entered.wait()
    finish.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert calls == 1
    assert isinstance(results[0], ExceptionGroup)
    assert all(result is results[0] for result in results)
    assert results[0].exceptions[0].__cause__ is failure
    assert scope.state == "close_failed"


@pytest.mark.asyncio
async def test_cancelled_close_waiter_does_not_abandon_owned_cleanup() -> None:
    scope = LifetimeScope("cancelled-waiter")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def cleanup() -> None:
        entered.set()
        await release.wait()
        calls.append("released")

    scope.register(cleanup)
    waiter = asyncio.create_task(scope.close())
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await asyncio.wait_for(scope.close(), timeout=1)

    assert calls == ["released"]
    assert scope.is_closed


@pytest.mark.asyncio
async def test_scope_drains_tasks_before_releasing_handles() -> None:
    scope = LifetimeScope("drain")
    started = asyncio.Event()
    cancelling = asyncio.Event()
    drained = asyncio.Event()
    calls: list[str] = []

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await drained.wait()
            calls.append("task-drained")

    scope.start_task(worker, label="worker")
    scope.register(lambda: calls.append("handle-released"))
    await started.wait()
    closing = asyncio.create_task(scope.close())
    await cancelling.wait()
    assert scope.state == "closing"
    assert calls == []
    with pytest.raises(RuntimeError, match="不允许注册"):
        scope.start_task(worker, label="late")

    drained.set()
    await closing

    assert calls == ["task-drained", "handle-released"]
    assert scope.snapshot().task_count == 0


@pytest.mark.asyncio
async def test_parent_task_can_drain_child_task_during_close() -> None:
    parent = LifetimeScope("parent")
    child = parent.child("child")
    started = asyncio.Event()
    calls: list[str] = []

    async def child_worker() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            calls.append("child-task")

    child_task = child.start_task(child_worker, label="worker")

    async def parent_worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.gather(child_task, return_exceptions=True)
            calls.append("parent-task")

    parent.start_task(parent_worker, label="worker")
    parent.register(lambda: calls.append("parent-handle"))
    await started.wait()
    await asyncio.wait_for(parent.close(), timeout=1)

    assert calls == ["child-task", "parent-task", "parent-handle"]
    assert child.is_closed and parent.is_closed


@pytest.mark.asyncio
async def test_task_and_handle_failure_are_reported_without_skipping_release(monkeypatch) -> None:
    scope = LifetimeScope("failures")
    reported: list[dict[str, object]] = []
    monkeypatch.setattr(asyncio.get_running_loop(), "call_exception_handler", reported.append)
    calls: list[str] = []

    async def broken_task() -> None:
        raise OSError("task failure")

    def broken_handle() -> None:
        raise ValueError("handle failure")

    task = scope.start_task(broken_task, label="broken-worker")
    scope.register(lambda: calls.append("released"))
    scope.register(broken_handle, label="broken-handle")
    with pytest.raises(OSError, match="task failure"):
        await task
    await asyncio.sleep(0)

    with pytest.raises(ExceptionGroup) as captured:
        await scope.close()
    assert [str(error.__cause__) for error in captured.value.exceptions] == [
        "task failure", "handle failure",
    ]
    assert calls == ["released"]
    assert scope.state == "close_failed"
    assert scope.snapshot().resource_count == 1
    assert scope.snapshot().task_count == 1
    task_reports = [item for item in reported if item.get("task") is task]
    assert len(task_reports) == 1
    assert task_reports[0]["exception"] is task.exception()


@pytest.mark.asyncio
async def test_cancelled_cleanup_is_a_failed_handle_and_can_be_retried() -> None:
    scope = LifetimeScope("cancelled-cleanup")
    calls = 0

    async def cleanup() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError("cleanup cancelled")

    scope.register(cleanup, label="cancelled")
    with pytest.raises(ExceptionGroup) as captured:
        await scope.close()
    assert isinstance(captured.value.exceptions[0].__cause__, asyncio.CancelledError)
    assert scope.state == "close_failed"
    assert scope.snapshot().resource_count == 1

    await scope.close()
    assert calls == 2
    assert scope.is_closed
