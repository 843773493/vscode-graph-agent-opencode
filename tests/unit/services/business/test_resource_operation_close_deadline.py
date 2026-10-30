"""资源operation关闭的总预算、原错误与精确占用保全。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from app.services.business.terminal_resources.operations import TerminalOperations
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)


@pytest.fixture(params=["terminal", "browser"])
def bundle(request: pytest.FixtureRequest, tmp_path: Path) -> TerminalOwnerFixture | BrowserOwnerFixture:
    return (build_terminal_owner_fixture if request.param == "terminal" else build_browser_owner_fixture)(tmp_path)


@pytest.fixture
async def advance_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    loop = asyncio.get_running_loop()
    real_time = loop.time
    offset = 0.0

    def advance(seconds: float) -> None:
        nonlocal offset
        offset += seconds

    monkeypatch.setattr(loop, "time", lambda: real_time() + offset)
    return advance


@pytest.mark.parametrize("release_failure", [False, True])
async def test_close_uses_one_budget_preserves_completed_error_and_retries_original_leases(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, advance_clock: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch, release_failure: bool,
) -> None:
    owner = bundle.owner
    assert owner is not None
    resources = [str((await bundle.create())["resource_id"]) for _ in range(3)]
    failed, early, blocked = resources
    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:close-budget", operation_id="close-budget") as operation:
        for rid in resources:
            if isinstance(bundle, TerminalOwnerFixture):
                await operation.read_terminal(rid)
            else:
                await operation.perform(rid, "navigate", {"url": "https://close-budget.test"})
        leases = {rid: bundle.leases.leases_for_resource(rid)[0] for rid in resources}
        release = bundle.leases.release
        original = TimeoutError("账本释放自己的超时")
        completed = asyncio.Event()

        def fail_one(lease_id: str):
            if lease_id == leases[failed].lease_id:
                completed.set()
                if release_failure:
                    raise original
            return release(lease_id)

        with monkeypatch.context() as patch:
            patch.setattr(bundle.leases, "release", fail_one)
            async with AsyncExitStack() as holders:
                early_lock, blocked_lock = owner._resource_lock(early), owner._resource_lock(blocked)
                await holders.enter_async_context(blocked_lock)
                await early_lock.acquire()
                closing = asyncio.create_task(operation.close())
                try:
                    await asyncio.wait_for(completed.wait(), 1)
                    assert not closing.done()
                    advance_clock(20)
                    early_lock.release()
                    async with asyncio.timeout(1):
                        while early in operation._lease_refs:
                            await asyncio.sleep(0)
                    assert not closing.done()
                    advance_clock(11)
                    with pytest.raises(ExceptionGroup if release_failure else TimeoutError) as caught:
                        await asyncio.wait_for(asyncio.shield(closing), 1)
                    if release_failure:
                        assert caught.value.exceptions[0] is original
                        timeout = caught.value.exceptions[1]
                        assert len(caught.value.exceptions) == 2
                    else:
                        timeout = caught.value
                    assert isinstance(timeout, TimeoutError) and "总截止时间" in str(timeout)
                    assert "operation_id=close-budget" in str(timeout) and blocked in str(timeout)
                    assert str(bundle.root) not in str(timeout)
                    expected_remaining = {blocked, failed} if release_failure else {blocked}
                    assert set(operation._lease_refs) == expected_remaining
                    cold = type(bundle.leases)(state_path=bundle.leases._state_path)
                    for rid, lease in leases.items():
                        expected = "active" if rid in expected_remaining else "released"
                        assert cold.get_lease(lease.lease_id).status == bundle.leases.get_lease(lease.lease_id).status == expected
                    assert not blocked_lock._waiters
                finally:
                    if early_lock.locked():
                        early_lock.release()
                    closing.cancel()
                    await asyncio.gather(closing, return_exceptions=True)
        # 释放锁本身不补做后台释放；只有原operation的显式重试结清原lease。
        await asyncio.sleep(0)
        assert set(operation._lease_refs) == expected_remaining
        await operation.close()
        await operation.close()
        assert operation._lease_refs == {}
        assert all(bundle.leases.get_lease(lease.lease_id).status == "released" for lease in leases.values())


async def test_close_preserves_underlying_timeout_identity_without_mistaking_it_for_budget(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = bundle.owner
    assert owner is not None
    rid = str((await bundle.create())["resource_id"])
    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:close-original", operation_id="close-original") as operation:
        if isinstance(bundle, TerminalOwnerFixture):
            await operation.read_terminal(rid)
        else:
            await operation.perform(rid, "navigate", {"url": "https://close-original.test"})
        lease, = bundle.leases.leases_for_resource(rid)
        original = TimeoutError("原持久释放错误")

        def fail(_lease_id: str):
            raise original

        with monkeypatch.context() as patch:
            patch.setattr(bundle.leases, "release", fail)
            with pytest.raises(TimeoutError) as caught:
                await operation.close()
        assert caught.value is original
        assert operation._lease_refs == {rid: lease.lease_id}
        await operation.close()
        assert bundle.leases.get_lease(lease.lease_id).status == "released"


@pytest.mark.parametrize("failure_count", [0, 1, 2])
async def test_close_cancellation_preserves_completed_errors_and_retries_original_leases(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    failure_count: int,
) -> None:
    owner = bundle.owner
    assert owner is not None
    resources = [str((await bundle.create())["resource_id"]) for _ in range(4)]
    first, second, healthy, blocked = resources
    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:close-cancel", operation_id="close-cancel") as operation:
        for rid in resources:
            if isinstance(operation, TerminalOperations):
                await operation.read_terminal(rid)
            else:
                await operation.perform(rid, "navigate", {"url": "https://close-cancel.test"})
        leases = {rid: bundle.leases.leases_for_resource(rid)[0] for rid in resources}
        ledger_ids = set(bundle.leases._leases)
        failures = dict(zip((first, second), (OSError("原账本释放失败"), TimeoutError("原持久层超时")), strict=True))
        failures = dict(tuple(failures.items())[:failure_count])
        release = bundle.leases.release
        first_finished = asyncio.Event()

        def releasing(lease_id: str):
            if lease_id == leases[first].lease_id:
                first_finished.set()
            for rid, error in failures.items():
                if lease_id == leases[rid].lease_id:
                    raise error
            return release(lease_id)

        with monkeypatch.context() as patch:
            patch.setattr(bundle.leases, "release", releasing)
            blocked_lock = owner._resource_lock(blocked)
            async with blocked_lock:
                first_lock = owner._resource_lock(first)
                await first_lock.acquire()
                closing = asyncio.create_task(operation.close())
                try:
                    async with asyncio.timeout(1):
                        while healthy in operation._lease_refs:
                            await asyncio.sleep(0)
                    # 第一项最后完成，错误仍须按资源输入顺序保留。
                    first_lock.release()
                    await asyncio.wait_for(first_finished.wait(), 1)
                    assert not closing.done()
                    closing.cancel("保留调用方取消消息")
                    with pytest.raises(asyncio.CancelledError, match="^保留调用方取消消息$") as caught:
                        await closing
                    assert closing.cancelled()
                    cause = caught.value.__cause__
                    if failures:
                        assert isinstance(cause, BaseExceptionGroup)
                        assert cause.exceptions == tuple(failures.values())
                    else:
                        assert cause is None
                    expected_refs = {rid: leases[rid].lease_id for rid in (*failures, blocked)}
                    assert operation._lease_refs == expected_refs
                    cold = type(bundle.leases)(state_path=bundle.leases._state_path)
                    for rid, lease in leases.items():
                        current = cold.get_lease(lease.lease_id)
                        assert current is not None
                        assert current.status == ("active" if rid in expected_refs else "released")
                    assert not blocked_lock._waiters
                finally:
                    if first_lock.locked():
                        first_lock.release()
                    closing.cancel()
                    await asyncio.gather(closing, return_exceptions=True)
        # 解锁不补做；同一operation只在显式重试时结清原占用。
        await asyncio.sleep(0)
        assert operation._lease_refs == expected_refs
        await operation.close()
        await operation.close()
        assert operation._lease_refs == {}
        cold = type(bundle.leases)(state_path=bundle.leases._state_path)
        assert set(cold._leases) == ledger_ids
        for lease in leases.values():
            current = cold.get_lease(lease.lease_id)
            assert current is not None and current.status == "released"
