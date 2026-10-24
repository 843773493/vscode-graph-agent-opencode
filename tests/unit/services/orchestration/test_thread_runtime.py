"""ThreadRuntime owner 的 lease、single-flight 与 scope close 合同。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

import pytest

from app.core.lifecycle import LifetimeScope
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.orchestration.thread_residency import (
    THREAD_IDLE_UNLOAD_SECONDS,
    THREAD_IDLE_UNLOAD_WAIT_SECONDS,
    ThreadResidencyTracker,
)


@pytest.fixture
def observed_closures(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []

    def observe(operation_id: str, phase: str, *, details: Mapping[str, object] | None = None) -> None:
        assert phase == "thread.runtime.closed"
        assert details is not None
        observations.append({"operation_id": operation_id, **details})

    monkeypatch.setattr("app.services.orchestration.thread_residency.observe_phase_sync", observe)
    return observations


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


async def test_runtime_binding_distinguishes_restart_and_rebinds_each_lease() -> None:
    first, cold = _tracker(_FakeClock()), _tracker(_FakeClock())
    seen: list[str] = []
    first.add_acquire_listener(lambda _sid, _tid, token: seen.append(token))
    pair = MutationIntentOwner("ses-a", "thread-a")
    admission = _admission(pair.session_id, pair.thread_id)
    lease = await first.acquire(pair, admission, "v1", lambda *_: object())
    first_token = first.runtime_binding_token(lease.generation)
    assert first.is_current_runtime_binding(pair.session_id, pair.thread_id, first_token)
    lease.release()
    second = await first.acquire(pair, admission, "v1", lambda *_: object())
    assert seen == [first_token, first_token]
    second.release()
    restarted = await cold.acquire(pair, admission, "v1", lambda *_: object())
    assert restarted.generation == lease.generation == 1
    assert cold.runtime_binding_token(restarted.generation) != first_token
    assert not cold.is_current_runtime_binding(pair.session_id, pair.thread_id, first_token)
    restarted.release()
    await first.close()
    assert not first.is_current_runtime_binding(pair.session_id, pair.thread_id, first_token)
    await cold.close()


async def test_acquire_binding_failure_does_not_publish_or_leak_runtime_lease() -> None:
    tracker = _tracker(_FakeClock())
    reject = True
    calls = []

    def bind(sid: str, tid: str, token: str) -> None:
        calls.append((sid, tid, token))
        if reject:
            raise OSError("关联写入未提交")

    tracker.add_acquire_listener(bind)
    pair = MutationIntentOwner("ses-a", "thread-a")
    admission = _admission(pair.session_id, pair.thread_id)
    with pytest.raises(OSError, match="关联写入未提交"):
        await tracker.acquire(pair, admission, "v1", lambda *_: object())
    assert tracker.snapshot(pair.session_id, pair.thread_id).execution_state == "idle"
    reject = False
    lease = await tracker.acquire(pair, admission, "v1", lambda *_: object())
    assert lease.generation == 1 and calls[0] == calls[1]
    lease.release()
    await tracker.close()



@pytest.mark.asyncio
@pytest.mark.parametrize("failed_threads", [("thread-a",), ("thread-a", "thread-b")])
async def test_sweep_attempts_other_owners_before_reporting_and_retries_failed_scopes(
    failed_threads: tuple[str, ...],
) -> None:
    clock = _FakeClock()
    tracker = _tracker(clock)
    scopes: dict[str, LifetimeScope] = {}
    attempts: list[str] = []
    failures = {thread_id: ConnectionError(f"资源回执丢失: {thread_id}") for thread_id in failed_threads}

    async def release(session_id: str, thread_id: str, generation: int) -> None:
        attempts.append(thread_id)
        if attempts.count(thread_id) == 1 and thread_id in failures:
            raise failures[thread_id]

    def build(session_id: str, thread_id: str, scope: LifetimeScope) -> object:
        scopes[thread_id] = scope
        return object()

    tracker.add_unload_listener(release)
    for thread_id in ("thread-a", "thread-b", "thread-c"):
        lease = await tracker.acquire(
            MutationIntentOwner("ses-a", thread_id),
            _admission("ses-a", thread_id), "config-v1", build,
        )
        lease.release()
    clock.advance(THREAD_IDLE_UNLOAD_SECONDS)

    expected_error = ConnectionError if len(failed_threads) == 1 else ExceptionGroup
    with pytest.raises(expected_error) as captured:
        await tracker.sweep()
    reported = captured.value.exceptions if isinstance(captured.value, ExceptionGroup) else (captured.value,)
    assert tuple(reported) == tuple(failures.values()), "必须保留每个 owner 的原始异常"
    for thread_id, error in failures.items():
        assert any(f"thread_id={thread_id}" in note and "session_id=ses-a" in note for note in error.__notes__)
    assert attempts == ["thread-a", "thread-b", "thread-c"]
    for thread_id, scope in scopes.items():
        assert scope.state == ("open" if thread_id in failures else "closed")
        assert tracker.snapshot("ses-a", thread_id).residency == (
            "resident" if thread_id in failures else "cold"
        )
        if thread_id in failures:
            assert tracker.snapshot("ses-a", thread_id).cold_eligible is True

    recovered = await tracker.sweep()
    assert {item.thread_id for item in recovered} == set(failed_threads)
    assert attempts == ["thread-a", "thread-b", "thread-c", *failed_threads]
    assert all(scope.state == "closed" for scope in scopes.values())
    assert tracker._states == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["listener", "scope"])
@pytest.mark.parametrize("fails_later", [False, True])
async def test_sweep_wait_budget_keeps_single_unload_and_other_threads_progress(
    phase: str, fails_later: bool,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _FakeClock()
    tracker = _tracker(clock)
    entered, finish, deadline = asyncio.Event(), asyncio.Event(), asyncio.Event()
    scopes: dict[str, LifetimeScope] = {}
    attempts: list[str] = []
    native_wait = asyncio.wait
    wait_expired = False
    sweep: asyncio.Task[object] | None = None

    async def release(thread_id: str) -> None:
        attempts.append(thread_id)
        if thread_id == "thread-a" and attempts.count(thread_id) == 1:
            entered.set()
            await finish.wait()
            if fails_later:
                raise ConnectionError("超时后收到真实清理失败回执")

    async def listener(session_id: str, thread_id: str, generation: int) -> None:
        if phase == "listener":
            await release(thread_id)

    def build(session_id: str, thread_id: str, scope: LifetimeScope) -> object:
        scopes[thread_id] = scope
        if phase == "scope":
            scope.register(lambda: release(thread_id), label="受控释放")
        return object()

    async def controlled_wait(
        tasks: set[asyncio.Task[None]], *, timeout: float,
    ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
        nonlocal wait_expired
        if asyncio.current_task() is not sweep:
            return await native_wait(tasks, timeout=timeout)
        assert timeout == THREAD_IDLE_UNLOAD_WAIT_SECONDS == 30
        task, = tasks
        if "thread-a:" in task.get_name() and not wait_expired:
            # 外部触发期限到达，不缩短产品的等待预算或取消原清理任务。
            await deadline.wait()
            wait_expired = True
            if not task.done():
                return set(), tasks
        return await native_wait(tasks, timeout=timeout)

    monkeypatch.setattr("app.services.orchestration.thread_residency.asyncio.wait", controlled_wait)
    tracker.add_unload_listener(listener)
    generations: dict[str, int] = {}
    for thread_id in ("thread-a", "thread-b"):
        lease = await tracker.acquire(
            MutationIntentOwner("ses-a", thread_id),
            _admission("ses-a", thread_id), "config-v1", build,
        )
        generations[thread_id] = lease.generation
        lease.release()
    clock.advance(THREAD_IDLE_UNLOAD_SECONDS)
    sweep = asyncio.create_task(tracker.sweep())
    waiter = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        unload = tracker._states[("ses-a", "thread-a")].runtime_unload
        assert unload is not None
        deadline.set()
        with pytest.raises(TimeoutError, match="原卸载任务仍由 owner 持有") as timed_out:
            await asyncio.wait_for(sweep, timeout=5)
        assert "thread_id=thread-a" in str(timed_out.value)
        assert not unload.done()
        assert tracker.snapshot("ses-a", "thread-a").residency == "unloading"
        assert scopes["thread-a"].state != "closed"
        assert scopes["thread-b"].state == "closed"
        assert tracker.snapshot("ses-a", "thread-b").residency == "cold"
        assert await tracker.sweep() == ()
        assert attempts == ["thread-a", "thread-b"]
        assert tracker._states[("ses-a", "thread-a")].runtime_unload is unload

        waiter = asyncio.create_task(tracker.acquire(
            MutationIntentOwner("ses-a", "thread-a"),
            _admission("ses-a", "thread-a"), "config-v2", build,
        ))
        await asyncio.sleep(0)
        assert not waiter.done(), "卸载超时不能允许新 execution 复用旧 runtime"
        old_scope = scopes["thread-a"]
        finish.set()
        if fails_later:
            expected_error = ConnectionError if phase == "listener" else ExceptionGroup
            with pytest.raises(expected_error) as failure:
                await asyncio.wait_for(asyncio.shield(unload), timeout=5)
            with pytest.raises(type(failure.value)) as admission_failure:
                await asyncio.wait_for(waiter, timeout=5)
            assert admission_failure.value is failure.value
            assert "超时后收到真实清理失败回执" in caplog.text
            assert unload.get_name() in caplog.text
            assert tracker.snapshot("ses-a", "thread-a").cold_eligible is True
            await tracker.sweep()
            assert attempts == ["thread-a", "thread-b", "thread-a"]
        else:
            next_lease = await asyncio.wait_for(waiter, timeout=5)
            assert next_lease.generation > generations["thread-a"]
            assert scopes["thread-a"] is not old_scope
            next_lease.release()
        assert old_scope.state == "closed"
    finally:
        deadline.set()
        finish.set()
        await asyncio.gather(sweep, *([waiter] if waiter is not None else []), return_exceptions=True)
        pending = [state.runtime_unload for state in tracker._states.values() if state.runtime_unload is not None]
        await asyncio.gather(*pending, return_exceptions=True)
        await tracker.close()


@pytest.mark.asyncio
async def test_cancelling_sweep_keeps_original_unload_owned_until_close() -> None:
    clock = _FakeClock()
    tracker = _tracker(clock)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls: list[int] = []
    scopes: list[LifetimeScope] = []

    async def listener(session_id: str, thread_id: str, generation: int) -> None:
        calls.append(generation)
        entered.set()
        await finish.wait()

    def build(session_id: str, thread_id: str, scope: LifetimeScope) -> object:
        scopes.append(scope)
        return object()

    tracker.add_unload_listener(listener)
    lease = await tracker.acquire(
        MutationIntentOwner("ses-a", "thread-a"),
        _admission("ses-a", "thread-a"), "config-v1", build,
    )
    generation = lease.generation
    lease.release()
    clock.advance(THREAD_IDLE_UNLOAD_SECONDS)
    sweep = asyncio.create_task(tracker.sweep())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        unload = tracker._states[("ses-a", "thread-a")].runtime_unload
        assert unload is not None
        sweep.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweep
        assert not unload.done()
        assert tracker.snapshot("ses-a", "thread-a").residency == "unloading"
        finish.set()
        await asyncio.wait_for(tracker.close(), timeout=5)
        assert unload.done() and not unload.cancelled()
        assert calls == [generation]
        assert scopes[0].state == "closed"
        assert tracker._states == {}
    finally:
        finish.set()
        await asyncio.gather(sweep, return_exceptions=True)
        await tracker.close()


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
    async with first, second:

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
    async with first, second:

        assert set(build_owners) == {
            ("ses-a", "same-thread-id"),
            ("ses-b", "same-thread-id"),
        }
        assert first.runtime == ("ses-a", "same-thread-id")
        assert second.runtime == ("ses-b", "same-thread-id")
        assert {first.generation, second.generation} == {1, 2}
        assert tracker.is_current_generation("ses-a", "same-thread-id", first.generation)
        assert tracker.is_current_generation("ses-b", "same-thread-id", second.generation)
        assert not tracker.is_current_generation("ses-a", "same-thread-id", second.generation)
        assert not tracker.is_current_generation("ses-b", "same-thread-id", first.generation)
        first.release()
        second.release()


@pytest.mark.asyncio
async def test_active_lease_blocks_unload_then_starts_idle_on_release(
    observed_closures: list[dict[str, object]],
) -> None:
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
    async with lease:
        runtime_object_id = id(lease.runtime)
        scope = scopes[0]
        clock.advance(THREAD_IDLE_UNLOAD_SECONDS * 2)
        active = tracker.snapshot("ses-a", "thread-a")
        assert active.execution_state == "active"
        assert active.cold_eligible is False
        assert await tracker.sweep() == ()
        assert released == []
        assert observed_closures == []

        lease.release()
        assert tracker.snapshot("ses-a", "thread-a").idle_seconds == 0
        clock.advance(THREAD_IDLE_UNLOAD_SECONDS - 1)
        assert tracker.snapshot("ses-a", "thread-a").cold_eligible is False
        clock.advance(1)
        assert tracker.snapshot("ses-a", "thread-a").cold_eligible is True
        await tracker.sweep()

        assert released == ["closed"]
        assert scope.state == "closed"
        assert observed_closures == [{
            "operation_id": "thread-a", "session_id": "ses-a", "thread_id": "thread-a",
            "generation": lease.generation, "runtime_object_id": runtime_object_id,
            "scope_object_id": id(scope), "scope_state": "closed",
        }]
        assert tracker.snapshot("ses-a", "thread-a").residency == "cold"
        assert tracker.is_current_generation("ses-a", "thread-a", lease.generation) is False
        lease.release()
        with pytest.raises(RuntimeError, match="已释放"):
            _ = lease.runtime


@pytest.mark.asyncio
async def test_scope_close_failure_fences_old_runtime_and_retries_before_rebuild(
    observed_closures: list[dict[str, object]],
) -> (
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
    async with lease:
        generation = lease.generation
        lease.release()
        clock.advance(THREAD_IDLE_UNLOAD_SECONDS)

        with pytest.raises(ExceptionGroup, match="LifetimeScope close 失败"):
            await tracker.sweep()
        assert tracker.is_current_generation("ses-a", "thread-a", generation) is False
        assert scopes[0].state == "close_failed"
        assert observed_closures == [], "失败的 close 不能虚报实例已释放"
        close_failed = tracker.snapshot("ses-a", "thread-a")
        assert close_failed.residency == "unloading"
        assert close_failed.cold_eligible is True

        await tracker.sweep()
        assert scopes[0].state == "closed"
        assert len(observed_closures) == 1
        assert observed_closures[0]["generation"] == generation
        assert observed_closures[0]["scope_state"] == "closed"
        cold = tracker.snapshot("ses-a", "thread-a")
        assert cold.residency == "cold"

        rebuilt = await tracker.acquire(
            MutationIntentOwner("ses-a", "thread-a"),
            _admission("ses-a", "thread-a"),
            "config-v1",
            build,
        )
        async with rebuilt:
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
    async with first:
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
        async with second:
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
    async with lease:
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
    async with lease:
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
    async with retry:
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
