from __future__ import annotations

import asyncio

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
