"""真实 runtime 准入/Session 排空的总等待预算与迟到回执。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
import pytest_asyncio

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


@pytest_asyncio.fixture
async def advance(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    loop = asyncio.get_running_loop()
    original = loop.time
    offset = 0.0

    def move(seconds: float) -> None:
        nonlocal offset
        offset += seconds

    monkeypatch.setattr(loop, "time", lambda: original() + offset)
    return move


async def _tick() -> None:
    # 只让已就绪任务和定时器各推进一轮，不缩短生产等待预算。
    for _ in range(8):
        await asyncio.sleep(0)


async def _acquire(
    tracker: ThreadResidencyTracker, build: ThreadRuntimeBuilder[object],
    *, thread: str = "thread-a", revision: str = "v1",
) -> ThreadRuntimeLease[object]:
    return await tracker.acquire(
        MutationIntentOwner("session-a", thread),
        ExecutionAdmission(
            session_id="session-a", thread_id=thread, execution_id="execution-unit",
            job_id="job-unit", admission_idempotency_key="admission-unit",
            execution_origin="user_turn", turn_id="turn-unit",
        ),
        revision, build,
    )


@pytest.mark.parametrize("phase", ["listener", "scope"])
@pytest.mark.parametrize("fails_later", [False, True])
async def test_admission_deadline_preserves_unload_and_late_receipt(
    tracker: ThreadResidencyTracker, advance: Callable[[float], None],
    phase: str, fails_later: bool,
) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()
    scopes: list[LifetimeScope] = []
    attempts = 0

    async def release() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await finish.wait()
            if fails_later:
                raise ConnectionError("真实迟到释放失败")

    async def listener(_sid: str, _tid: str, _generation: int) -> None:
        if phase == "listener":
            await release()

    def build(_sid: str, _tid: str, scope: LifetimeScope) -> object:
        scopes.append(scope)
        if phase == "scope":
            scope.register(release, label="受控释放")
        return object()

    tracker.add_unload_listener(listener)
    lease = await _acquire(tracker, build)
    lease.release()
    waiting = asyncio.create_task(_acquire(tracker, build, revision="v2"))
    retry = None
    try:
        await entered.wait()
        state = tracker._states[("session-a", "thread-a")]
        unload = state.runtime_unload
        assert unload is not None
        advance(30)
        await _tick()
        assert waiting.done(), "准入不能无限等待原卸载任务"
        with pytest.raises(TimeoutError, match="session_id=session-a.*thread_id=thread-a"):
            await waiting
        assert state.runtime_unload is unload and not unload.done()
        assert state.pending_runtime_leases == 0 and len(scopes) == 1
        assert scopes[0].state != "closed" and attempts == 1
        retry = asyncio.create_task(_acquire(tracker, build, revision="v2"))
        await _tick()
        assert not retry.done() and state.runtime_unload is unload
        assert attempts == 1
        finish.set()
        if fails_later:
            if phase == "scope":
                with pytest.raises(ExceptionGroup) as failure:
                    await retry
                original = failure.value.exceptions[0].__cause__
                assert isinstance(original, ConnectionError) and str(original) == "真实迟到释放失败"
            else:
                with pytest.raises(ConnectionError, match="^真实迟到释放失败$"):
                    await retry
            assert len(scopes) == 1
            lease = await _acquire(tracker, build, revision="v2")
        else:
            lease = await retry
        assert lease.generation == 2 and len(scopes) == 2
        lease.release()
    finally:
        finish.set()
        results = await asyncio.gather(waiting, *([retry] if retry is not None else []), return_exceptions=True)
        for result in results:
            if isinstance(result, ThreadRuntimeLease):
                result.release()
        await tracker.close()


async def test_initial_builder_timeout_keeps_single_flight_and_scope(
    tracker: ThreadResidencyTracker, advance: Callable[[float], None],
) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()
    scopes: list[LifetimeScope] = []

    async def build(_sid: str, _tid: str, scope: LifetimeScope) -> object:
        scopes.append(scope)
        entered.set()
        await finish.wait()
        return object()

    waiting = asyncio.create_task(_acquire(tracker, build))
    retry = None
    try:
        await entered.wait()
        state = tracker._states[("session-a", "thread-a")]
        original_build = state.runtime_build
        advance(30)
        await _tick()
        assert waiting.done(), "首次构建也必须计入同一准入等待预算"
        with pytest.raises(TimeoutError):
            await waiting
        assert original_build is not None and not original_build.done()
        assert state.runtime_build is original_build and scopes[0].state == "open"
        retry = asyncio.create_task(_acquire(tracker, build))
        await _tick()
        assert state.runtime_build is original_build and len(scopes) == 1
        finish.set()
        lease = await retry
        assert lease.generation == 1
        lease.release()
    finally:
        finish.set()
        for result in await asyncio.gather(waiting, *([retry] if retry is not None else []), return_exceptions=True):
            if isinstance(result, ThreadRuntimeLease):
                result.release()
        await tracker.close()


async def test_session_drain_shares_one_deadline_across_threads_and_retry(
    tracker: ThreadResidencyTracker, advance: Callable[[float], None],
) -> None:
    entered = {thread: asyncio.Event() for thread in ("thread-a", "thread-b")}
    finish = {thread: asyncio.Event() for thread in entered}
    scopes: dict[str, LifetimeScope] = {}
    attempts: list[str] = []

    async def listener(_sid: str, thread: str, _generation: int) -> None:
        attempts.append(thread)
        entered[thread].set()
        await finish[thread].wait()

    def build(_sid: str, thread: str, scope: LifetimeScope) -> object:
        scopes[thread] = scope
        return object()

    tracker.add_unload_listener(listener)
    for thread in entered:
        lease = await _acquire(tracker, build, thread=thread)
        lease.release()
    closing = asyncio.create_task(tracker.close(session_id="session-a"))
    retry = None
    try:
        await entered["thread-a"].wait()
        advance(20)
        finish["thread-a"].set()
        await entered["thread-b"].wait()
        state = tracker._states[("session-a", "thread-b")]
        unload = state.runtime_unload
        advance(9)
        await _tick()
        assert not closing.done()
        advance(1)
        await _tick()
        assert closing.done(), "Session 排空不能为每个 Thread 重置等待预算"
        with pytest.raises(ExceptionGroup) as failure:
            await closing
        assert any(isinstance(error, TimeoutError) and "thread_id=thread-b" in str(error)
                   for error in failure.value.exceptions)
        assert unload is not None and not unload.done() and state.runtime_unload is unload
        assert scopes["thread-a"].state == "closed" and scopes["thread-b"].state == "open"
        retry = asyncio.create_task(tracker.close(session_id="session-a"))
        await _tick()
        assert state.runtime_unload is unload and attempts == ["thread-a", "thread-b"]
        finish["thread-b"].set()
        await retry
        assert scopes["thread-b"].state == "closed"
        assert tracker.snapshot("session-a", "thread-b").residency == "cold"
    finally:
        for event in finish.values():
            event.set()
        await asyncio.gather(closing, *([retry] if retry is not None else []), return_exceptions=True)
        await tracker.close()


async def test_session_drain_timeout_does_not_close_pending_builder_scope(
    tracker: ThreadResidencyTracker, advance: Callable[[float], None],
) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()
    scopes: list[LifetimeScope] = []

    async def build(_sid: str, _tid: str, scope: LifetimeScope) -> object:
        scopes.append(scope)
        entered.set()
        await finish.wait()
        assert scope.state == "open"
        return object()

    waiting = asyncio.create_task(_acquire(tracker, build))
    closing = None
    try:
        await entered.wait()
        closing = asyncio.create_task(tracker.close(session_id="session-a"))
        await _tick()
        advance(30)
        await _tick()
        assert closing.done(), "排空等待构建也需要截止时间"
        with pytest.raises(ExceptionGroup):
            await closing
        assert scopes[0].state == "open"
        state = tracker._states[("session-a", "thread-a")]
        assert state.runtime_build is not None and not state.runtime_build.done()
        finish.set()
        for result in await asyncio.gather(waiting, return_exceptions=True):
            if isinstance(result, ThreadRuntimeLease):
                result.release()
        await tracker.close(session_id="session-a")
        assert scopes[0].state == "closed"
    finally:
        finish.set()
        # 原构建成功会交付真实 lease；先归还它，排空任务才可能完成。
        for result in await asyncio.gather(waiting, return_exceptions=True):
            if isinstance(result, ThreadRuntimeLease):
                result.release()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await tracker.close()
