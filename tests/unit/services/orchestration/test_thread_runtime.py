"""ThreadRuntime owner 的 lease、single-flight 与 scope close 合同。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.orchestration.thread_residency import (
    THREAD_IDLE_UNLOAD_SECONDS,
    ThreadResidencyTracker,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _admission(session_id: str, thread_id: str) -> ExecutionAdmission:
    return ExecutionAdmission(
        session_id=session_id,
        thread_id=thread_id,
        execution_id="exec-unit",
        job_id="job-unit",
        admission_idempotency_key="adm-unit",
        execution_origin="user_turn",
        turn_id="turn-unit",
    )


def _tracker(clock: _FakeClock) -> ThreadResidencyTracker:
    return ThreadResidencyTracker(clock=clock, idle_timeout_seconds=30 * 60)


@pytest.mark.asyncio
async def test_concurrent_admission_builds_one_runtime_for_exact_owner() -> None:
    tracker = _tracker(_FakeClock())
    started = asyncio.Event()
    finish_build = asyncio.Event()
    build_owners: list[tuple[str, str]] = []

    async def build(session_id: str, thread_id: str, scope: LifetimeScope) -> object:
        build_owners.append((session_id, thread_id))
        started.set()
        await finish_build.wait()
        return object()

    first_task = asyncio.create_task(
        tracker.acquire(
            MutationIntentOwner("ses-a", "child-a"),
            _admission("ses-a", "child-a"),
            "config-v1",
            build,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    assert tracker.snapshot("ses-a", "child-a").residency == "loading"
    second_task = asyncio.create_task(
        tracker.acquire(
            MutationIntentOwner("ses-a", "child-a"),
            _admission("ses-a", "child-a"),
            "config-v1",
            build,
        )
    )
    finish_build.set()
    first, second = await asyncio.wait_for(
        asyncio.gather(first_task, second_task), timeout=5
    )

    assert build_owners == [("ses-a", "child-a")]
    assert first.runtime is second.runtime
    assert first.generation == second.generation == 1
    resident = tracker.snapshot("ses-a", "child-a")
    assert resident.generation == 1
    assert resident.residency == "resident"
    first.release()
    second.release()


@pytest.mark.asyncio
async def test_runtime_slots_are_isolated_by_session_and_thread_pair() -> None:
    tracker = _tracker(_FakeClock())
    build_owners: list[tuple[str, str]] = []

    def build(session_id: str, thread_id: str, scope: LifetimeScope) -> object:
        build_owners.append((session_id, thread_id))
        return session_id, thread_id

    first, second = await asyncio.gather(
        tracker.acquire(
            MutationIntentOwner("ses-a", "same-thread-id"),
            _admission("ses-a", "same-thread-id"),
            "config-v1",
            build,
        ),
        tracker.acquire(
            MutationIntentOwner("ses-b", "same-thread-id"),
            _admission("ses-b", "same-thread-id"),
            "config-v1",
            build,
        ),
    )

    assert set(build_owners) == {
        ("ses-a", "same-thread-id"),
        ("ses-b", "same-thread-id"),
    }
    assert first.runtime == ("ses-a", "same-thread-id")
    assert second.runtime == ("ses-b", "same-thread-id")
    assert first.generation == second.generation == 1
    first.release()
    second.release()


@pytest.mark.asyncio
async def test_active_lease_blocks_unload_then_starts_idle_on_release() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock)
    released: list[str] = []
    scopes: list[LifetimeScope] = []

    def build(session_id: str, thread_id: str, scope: LifetimeScope) -> str:
        scopes.append(scope)
        scope.register(lambda: released.append("closed"), label="测试资源")
        return "runtime"

    lease = await tracker.acquire(
        MutationIntentOwner("ses-a", "thread-a"),
        _admission("ses-a", "thread-a"),
        "config-v1",
        build,
    )
    scope = scopes[0]
    clock.advance(THREAD_IDLE_UNLOAD_SECONDS * 2)
    active = tracker.snapshot("ses-a", "thread-a")
    assert active.execution_state == "active"
    assert active.cold_eligible is False
    assert await tracker.sweep() == ()
    assert released == []

    lease.release()
    assert tracker.snapshot("ses-a", "thread-a").idle_seconds == 0
    clock.advance(THREAD_IDLE_UNLOAD_SECONDS - 1)
    assert tracker.snapshot("ses-a", "thread-a").cold_eligible is False
    clock.advance(1)
    assert tracker.snapshot("ses-a", "thread-a").cold_eligible is True
    await tracker.sweep()

    assert released == ["closed"]
    assert scope.state == "closed"
    assert tracker.snapshot("ses-a", "thread-a").residency == "cold"
    assert tracker.is_current_generation("ses-a", "thread-a", lease.generation) is False
    lease.release()
    with pytest.raises(RuntimeError, match="已释放"):
        _ = lease.runtime


@pytest.mark.asyncio
async def test_scope_close_failure_fences_old_runtime_and_retries_before_rebuild() -> (
    None
):
    clock = _FakeClock()
    tracker = _tracker(clock)
    attempts = 0
    release_attempts = 0
    scopes: list[LifetimeScope] = []

    def build(session_id: str, thread_id: str, scope: LifetimeScope) -> str:
        nonlocal attempts
        attempts += 1
        scopes.append(scope)

        def release() -> None:
            nonlocal release_attempts
            release_attempts += 1
            if release_attempts == 1:
                raise RuntimeError("release failed once")

        scope.register(release, label="retryable resource")
        return f"runtime-{attempts}"

    lease = await tracker.acquire(
        MutationIntentOwner("ses-a", "thread-a"),
        _admission("ses-a", "thread-a"),
        "config-v1",
        build,
    )
    generation = lease.generation
    lease.release()
    clock.advance(THREAD_IDLE_UNLOAD_SECONDS)

    with pytest.raises(ExceptionGroup, match="LifetimeScope close 失败"):
        await tracker.sweep()
    assert tracker.is_current_generation("ses-a", "thread-a", generation) is False
    assert scopes[0].state == "close_failed"
    close_failed = tracker.snapshot("ses-a", "thread-a")
    assert close_failed.residency == "unloading"
    assert close_failed.cold_eligible is True

    await tracker.sweep()
    assert scopes[0].state == "closed"
    cold = tracker.snapshot("ses-a", "thread-a")
    assert cold.residency == "cold"

    rebuilt = await tracker.acquire(
        MutationIntentOwner("ses-a", "thread-a"),
        _admission("ses-a", "thread-a"),
        "config-v1",
        build,
    )
    assert rebuilt.generation == generation + 1
    assert rebuilt.runtime == "runtime-2"
    rebuilt.release()


@pytest.mark.asyncio
async def test_admission_waits_for_closing_scope_and_rebuilds_new_generation() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock)
    close_started = asyncio.Event()
    finish_close = asyncio.Event()
    build_count = 0
    scopes: list[LifetimeScope] = []

    def build(session_id: str, thread_id: str, scope: LifetimeScope) -> int:
        nonlocal build_count
        build_count += 1
        scopes.append(scope)
        if build_count == 1:

            async def close_resource() -> None:
                close_started.set()
                await finish_close.wait()

            scope.register(close_resource, label="held close")
        return build_count

    first = await tracker.acquire(
        MutationIntentOwner("ses-a", "thread-a"),
        _admission("ses-a", "thread-a"),
        "config-v1",
        build,
    )
    first_generation = first.generation
    first.release()
    assert tracker.snapshot("ses-a", "thread-a").residency == "resident"
    clock.advance(THREAD_IDLE_UNLOAD_SECONDS)
    sweep = asyncio.create_task(tracker.sweep())
    await asyncio.wait_for(close_started.wait(), timeout=5)

    assert tracker.snapshot("ses-a", "thread-a").residency == "unloading"
    assert tracker.is_current_generation("ses-a", "thread-a", first_generation) is False
    second_task = asyncio.create_task(
        tracker.acquire(
            MutationIntentOwner("ses-a", "thread-a"),
            _admission("ses-a", "thread-a"),
            "config-v1",
            build,
        )
    )
    await asyncio.sleep(0)
    assert second_task.done() is False

    finish_close.set()
    await asyncio.wait_for(sweep, timeout=5)
    second = await asyncio.wait_for(second_task, timeout=5)
    assert second.generation == first_generation + 1
    assert second.runtime == 2
    assert scopes[0].state == "closed"
    second.release()


@pytest.mark.asyncio
async def test_builder_failure_does_not_publish_generation_and_can_retry() -> None:
    tracker = _tracker(_FakeClock())
    attempts = 0

    def build(session_id: str, thread_id: str, scope: LifetimeScope) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("graph construction failed")
        return session_id, thread_id

    assert tracker.snapshot("ses-a", "thread-a").residency == "cold"
    with pytest.raises(RuntimeError, match="graph construction failed"):
        await tracker.acquire(
            MutationIntentOwner("ses-a", "thread-a"),
            _admission("ses-a", "thread-a"),
            "config-v1",
            build,
        )
    after_failure = tracker.snapshot("ses-a", "thread-a")
    assert after_failure.generation == 0
    assert after_failure.residency == "cold"

    lease = await tracker.acquire(
        MutationIntentOwner("ses-a", "thread-a"),
        _admission("ses-a", "thread-a"),
        "config-v1",
        build,
    )
    assert lease.generation == 1
    assert lease.runtime == ("ses-a", "thread-a")
    assert tracker.snapshot("ses-a", "thread-a").residency == "resident"
    lease.release()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_build() -> None:
    tracker = _tracker(_FakeClock())
    started = asyncio.Event()
    finish_build = asyncio.Event()
    calls = 0

    async def build(session_id: str, thread_id: str, scope: LifetimeScope) -> object:
        nonlocal calls
        calls += 1
        started.set()
        await finish_build.wait()
        return object()

    cancelled = asyncio.create_task(
        tracker.acquire(
            MutationIntentOwner("ses-a", "thread-a"),
            _admission("ses-a", "thread-a"),
            "config-v1",
            build,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    survivor = asyncio.create_task(
        tracker.acquire(
            MutationIntentOwner("ses-a", "thread-a"),
            _admission("ses-a", "thread-a"),
            "config-v1",
            build,
        )
    )
    finish_build.set()
    lease = await asyncio.wait_for(survivor, timeout=5)
    assert calls == 1
    assert lease.generation == 1
    lease.release()


@pytest.mark.asyncio
async def test_cancelled_last_waiter_logs_later_builder_failure(caplog) -> None:
    tracker = _tracker(_FakeClock())
    started = asyncio.Event()
    finish_build = asyncio.Event()

    async def build(session_id: str, thread_id: str, scope: LifetimeScope) -> object:
        started.set()
        await finish_build.wait()
        raise RuntimeError("detached builder failed")

    caplog.set_level(
        logging.ERROR,
        logger="app.services.orchestration.thread_residency",
    )
    waiter = asyncio.create_task(
        tracker.acquire(
            MutationIntentOwner("ses-a", "thread-a"),
            _admission("ses-a", "thread-a"),
            "config-v1",
            build,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    finish_build.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if any(
            "thread-runtime-builder:ses-a:thread-a" in record.message
            for record in caplog.records
        ):
            break
    else:
        pytest.fail("无人等待的 builder 失败没有记录到 owner 日志")

    failure = next(
        record
        for record in caplog.records
        if "thread-runtime-builder:ses-a:thread-a" in record.message
    )
    assert failure.exc_info is not None
    assert str(failure.exc_info[1]) == "detached builder failed"
    assert tracker.snapshot("ses-a", "thread-a").residency == "cold"

    retry = await tracker.acquire(
        MutationIntentOwner("ses-a", "thread-a"),
        _admission("ses-a", "thread-a"),
        "config-v1",
        lambda session_id, thread_id, scope: "recovered",
    )
    assert retry.runtime == "recovered"
    retry.release()


@pytest.mark.asyncio
async def test_cancelled_all_waiters_still_starts_idle_for_built_runtime() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock)
    started = asyncio.Event()
    finish_build = asyncio.Event()

    async def build(session_id: str, thread_id: str, scope: LifetimeScope) -> object:
        started.set()
        await finish_build.wait()
        return object()

    waiter = asyncio.create_task(
        tracker.acquire(
            MutationIntentOwner("ses-a", "thread-a"),
            _admission("ses-a", "thread-a"),
            "config-v1",
            build,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    finish_build.set()

    for _ in range(20):
        snapshot = tracker.snapshot("ses-a", "thread-a")
        if snapshot.generation == 1:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("被取消的 admission 后 runtime builder 未完成")

    assert snapshot.idle_seconds == 0
    clock.advance(THREAD_IDLE_UNLOAD_SECONDS)
    await tracker.sweep()
    assert tracker.snapshot("ses-a", "thread-a").residency == "cold"
