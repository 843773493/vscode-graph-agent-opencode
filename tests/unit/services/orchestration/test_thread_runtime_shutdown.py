"""批量关闭独立等待每个原 owner；挂起、取消和失败不饿死其它 Thread。"""

from __future__ import annotations

import asyncio

import pytest

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.orchestration.thread_residency import (
    ThreadResidencyTracker,
    ThreadRuntimeBuilder,
    ThreadRuntimeLease,
)


@pytest.fixture
def tracker() -> ThreadResidencyTracker:
    return ThreadResidencyTracker()


async def _acquire(
    tracker: ThreadResidencyTracker, thread: str, builder: ThreadRuntimeBuilder[object],
    *, session: str = "session-a",
) -> ThreadRuntimeLease[object]:
    return await tracker.acquire(
        MutationIntentOwner(session, thread),
        ExecutionAdmission(
            session_id=session, thread_id=thread, execution_id=f"execution-{thread}",
            job_id=f"job-{thread}", admission_idempotency_key=f"admission-{thread}",
            execution_origin="user_turn", turn_id=f"turn-{thread}",
        ), "v1", builder,
    )


async def _tick() -> None:
    for _ in range(32):
        await asyncio.sleep(0)


@pytest.mark.parametrize("session_only", [False, True])
@pytest.mark.parametrize("phase", ["build", "lease", "listener", "scope"])
async def test_close_starts_other_threads_while_original_owner_is_pending(
    tracker: ThreadResidencyTracker, session_only: bool, phase: str,
) -> None:
    entered, finish, peer_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    scopes: dict[str, LifetimeScope] = {}
    attempts: list[str] = []

    async def listener(_sid: str, thread: str, _generation: int) -> None:
        attempts.append(thread)
        if thread == "first" and phase == "listener":
            entered.set()
            await finish.wait()

    async def build(_sid: str, thread: str, scope: LifetimeScope) -> object:
        scopes[thread] = scope
        if thread == "first" and phase == "build":
            entered.set()
            await finish.wait()

        async def release() -> None:
            if thread == "first" and phase == "scope":
                entered.set()
                await finish.wait()
            elif thread == "peer":
                peer_closed.set()

        scope.register(release, label="原 Thread 资源")
        return object()

    tracker.add_unload_listener(listener)
    first = asyncio.create_task(_acquire(tracker, "first", build))
    first_lease = None
    closing = retry = None
    try:
        if phase == "build":
            await entered.wait()
        else:
            first_lease = await first
            if phase != "lease":
                first_lease.release()
        peer = await _acquire(tracker, "peer", build)
        peer.release()
        closing = asyncio.create_task(tracker.close(session_id="session-a" if session_only else None))
        await _tick()
        assert peer_closed.is_set(), "首项挂起时必须先完成其它 Thread 的真实 scope 清理"
        assert scopes["peer"].state == "closed"
        assert tracker.snapshot("session-a", "peer").residency == "cold"
        assert not closing.done() and scopes["first"].state != "closed"
        state = tracker._states[("session-a", "first")]
        original = state.runtime_build if phase == "build" else state.runtime_unload
        assert original is not None and not original.done()
        retry = asyncio.create_task(tracker.close(session_id="session-a" if session_only else None))
        await _tick()
        assert (state.runtime_build if phase == "build" else state.runtime_unload) is original
        assert attempts.count("first") == (0 if phase in {"build", "lease"} else 1)
        finish.set()
        if first_lease is not None:
            first_lease.release()
        result, = await asyncio.gather(first, return_exceptions=True)
        if isinstance(result, ThreadRuntimeLease):
            result.release()
        else:
            assert not session_only and isinstance(result, RuntimeError)
            assert "拒绝新 admission" in str(result)
        await asyncio.gather(closing, retry)
        assert scopes["first"].state == "closed"
        assert attempts.count("first") == attempts.count("peer") == 1
        assert not tracker._states
    finally:
        finish.set()
        for result in await asyncio.gather(first, return_exceptions=True):
            if isinstance(result, ThreadRuntimeLease):
                result.release()
        if first_lease is not None:
            first_lease.release()
        await asyncio.gather(*[task for task in (closing, retry) if task is not None], return_exceptions=True)
        await tracker.close()


@pytest.mark.parametrize("session_only", [False, True])
async def test_cancelled_close_waiter_keeps_original_unload_and_drains_its_waiters(
    tracker: ThreadResidencyTracker, session_only: bool,
) -> None:
    finish = asyncio.Event()
    attempts: list[str] = []
    scopes: dict[str, LifetimeScope] = {}

    async def listener(_sid: str, thread: str, _generation: int) -> None:
        attempts.append(thread)
        if thread == "first":
            await finish.wait()

    def build(_sid: str, thread: str, scope: LifetimeScope) -> object:
        scopes[thread] = scope
        return object()

    tracker.add_unload_listener(listener)
    for thread in ("first", "peer"):
        (await _acquire(tracker, thread, build)).release()
    closing = asyncio.create_task(tracker.close(session_id="session-a" if session_only else None))
    try:
        await _tick()
        original = tracker._states[("session-a", "first")].runtime_unload
        assert original is not None and not original.done()
        assert scopes["peer"].state == "closed"
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert tracker._states[("session-a", "first")].runtime_unload is original
        assert not original.done() and scopes["first"].state == "open"
        assert not any(task.get_name().startswith("thread-runtime-drain-wait:") for task in asyncio.all_tasks())
        finish.set()
        await tracker.close(session_id="session-a" if session_only else None)
        assert attempts == ["first", "peer"] and scopes["first"].state == "closed"
    finally:
        finish.set()
        await asyncio.gather(closing, return_exceptions=True)
        await tracker.close()


async def test_close_aggregates_original_failures_in_owner_order_and_retries(
    tracker: ThreadResidencyTracker,
) -> None:
    finish, peer_failed = asyncio.Event(), asyncio.Event()
    first_error, peer_error = ConnectionError("第一 owner 迟到失败"), ValueError("另一 owner 先失败")
    attempts: dict[str, int] = {}
    scopes: dict[str, LifetimeScope] = {}

    async def listener(_sid: str, thread: str, _generation: int) -> None:
        attempts[thread] = attempts.get(thread, 0) + 1
        if attempts[thread] != 1:
            return
        if thread == "first":
            await finish.wait()
            raise first_error
        peer_failed.set()
        raise peer_error

    def build(_sid: str, thread: str, scope: LifetimeScope) -> object:
        scopes[thread] = scope
        return object()

    tracker.add_unload_listener(listener)
    for thread in ("first", "peer"):
        (await _acquire(tracker, thread, build)).release()
    closing = asyncio.create_task(tracker.close())
    try:
        await _tick()
        assert peer_failed.is_set(), "后项原错误不能被前项等待隐藏"
        assert not closing.done()
        finish.set()
        with pytest.raises(ExceptionGroup) as failure:
            await closing
        assert failure.value.exceptions == (first_error, peer_error)
        assert all(scope.state == "open" for scope in scopes.values())
        await tracker.close()
        assert attempts == {"first": 2, "peer": 2}
        assert all(scope.state == "closed" for scope in scopes.values())
    finally:
        finish.set()
        await asyncio.gather(closing, return_exceptions=True)
        await tracker.close()


@pytest.mark.parametrize("phase", ["build", "lease", "listener", "scope"])
async def test_global_close_deadline_keeps_original_work_and_retries(
    tracker: ThreadResidencyTracker, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    from app.services.orchestration import thread_residency

    entered, finish, peer_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    scopes: dict[str, LifetimeScope] = {}
    attempts: list[str] = []

    async def listener(_session: str, thread: str, _generation: int) -> None:
        attempts.append(thread)
        if thread == "first" and phase == "listener":
            entered.set()
            await finish.wait()

    async def build(_session: str, thread: str, scope: LifetimeScope) -> object:
        scopes[thread] = scope
        if thread == "first" and phase == "build":
            entered.set()
            await finish.wait()

        async def release() -> None:
            if thread == "first" and phase == "scope":
                entered.set()
                await finish.wait()
            if thread == "peer":
                peer_closed.set()

        scope.register(release, label="原runtime关闭")
        return object()

    tracker.add_unload_listener(listener)
    first = asyncio.create_task(_acquire(tracker, "first", build))
    lease = None
    try:
        if phase == "build":
            await entered.wait()
        else:
            lease = await first
            if phase != "lease":
                lease.release()
        (await _acquire(tracker, "peer", build)).release()
        monkeypatch.setattr(thread_residency, "THREAD_RUNTIME_WAIT_SECONDS", 0.02)
        with pytest.raises(ExceptionGroup) as failure:
            await asyncio.wait_for(tracker.close(), 1)
        error, = failure.value.exceptions
        assert isinstance(error, TimeoutError)
        assert "shutdown-drain" in str(error) and "thread_id=first" in str(error)
        assert peer_closed.is_set() and scopes["peer"].is_closed
        state = tracker._states[("session-a", "first")]
        original = state.runtime_build if phase == "build" else state.runtime_unload
        assert original is not None and not original.done() and not scopes["first"].is_closed
        with pytest.raises(ExceptionGroup):
            await asyncio.wait_for(tracker.close(), 1)
        assert (state.runtime_build if phase == "build" else state.runtime_unload) is original
        assert attempts.count("first") == (0 if phase in {"build", "lease"} else 1)
        finish.set()
        if lease is not None:
            lease.release()
        result, = await asyncio.gather(first, return_exceptions=True)
        if isinstance(result, ThreadRuntimeLease):
            result.release()
        else:
            assert isinstance(result, RuntimeError) and "拒绝新 admission" in str(result)
        await asyncio.wait_for(tracker.close(), 1)
        assert scopes["first"].is_closed and not tracker._states
        assert attempts.count("first") == attempts.count("peer") == 1
    finally:
        finish.set()
        for result in await asyncio.gather(first, return_exceptions=True):
            if isinstance(result, ThreadRuntimeLease):
                result.release()
        if lease is not None:
            lease.release()
        await tracker.close()
