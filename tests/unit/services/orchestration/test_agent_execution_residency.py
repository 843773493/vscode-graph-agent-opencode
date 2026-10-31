"""闲置 worker 的真实 scope 清理、失败重试与关闭行为。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.lifecycle import LifetimeScope
from app.core.shielded_cleanup import run_cleanup_shielded
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.orchestration.agent_execution_service import AgentExecutionService
from app.services.orchestration.thread_residency import (
    THREAD_IDLE_UNLOAD_SECONDS,
    ThreadResidencyTracker,
)


@pytest.fixture
def residency_clock() -> list[float]:
    return [0.0]


@pytest.fixture
def residency_service(residency_clock: list[float], tmp_path: Path) -> AgentExecutionService:
    """仅替身未使用的执行依赖；residency、worker 和 scope 都用真实实现。"""
    return AgentExecutionService(
        config_service=MagicMock(), background_task_registry=MagicMock(),
        background_message_bus=MagicMock(), job_event_bus=MagicMock(),
        dependency_provider=MagicMock(), session_changes_service=MagicMock(),
        tool_selection_store=MagicMock(), message_stream_store=MagicMock(),
        workspace_id="00000000-4000-7000-8000-000000000001", workspace_root=tmp_path,
        session_catalog=MagicMock(), graph_binding_store=MagicMock(),
        residency_tracker=ThreadResidencyTracker(clock=lambda: residency_clock[0]),
        resource_activation_factory=MagicMock(), team_resource_subscriber=MagicMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fails_during_start", [False, True])
async def test_idle_worker_retries_failed_owner_on_original_interval_and_shuts_down(
    residency_service: AgentExecutionService,
    residency_clock: list[float],
    fails_during_start: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracker = residency_service._residency_tracker
    scopes: list[LifetimeScope] = []
    attempts = 0

    async def release(session_id: str, thread_id: str, generation: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("模拟资源 owner 暂时不可达")

    def build(session_id: str, thread_id: str, lifetime_scope: LifetimeScope) -> object:
        scopes.append(lifetime_scope)
        return object()

    tracker.add_unload_listener(release)
    lease = await tracker.acquire(
        MutationIntentOwner("ses-a", "thread-a"),
        ExecutionAdmission(
            session_id="ses-a", thread_id="thread-a", execution_id="exec-unit",
            job_id="job-unit", admission_idempotency_key="adm-unit",
            execution_origin="user_turn", turn_id="turn-unit",
        ),
        "config-v1", build,
    )
    lease.release()
    ticks: asyncio.Queue[float] = asyncio.Queue()
    permits: asyncio.Queue[None] = asyncio.Queue()
    native_sleep = asyncio.sleep

    async def controlled_sleep(delay: float) -> None:
        if delay != 60:
            await native_sleep(delay)
            return
        ticks.put_nowait(delay)
        await permits.get()

    monkeypatch.setattr("app.services.orchestration.agent_execution_service.asyncio.sleep", controlled_sleep)
    if fails_during_start:
        residency_clock[0] = THREAD_IDLE_UNLOAD_SECONDS
    worker: asyncio.Task[None] | None = None
    try:
        with caplog.at_level(logging.ERROR, logger="app.services.orchestration.agent_execution_service"):
            await residency_service.start()
            worker = residency_service._sweep_task
            assert worker is not None
            assert await asyncio.wait_for(ticks.get(), timeout=5) == 60
            if not fails_during_start:
                residency_clock[0] = THREAD_IDLE_UNLOAD_SECONDS
                permits.put_nowait(None)
                assert await asyncio.wait_for(ticks.get(), timeout=5) == 60
            assert attempts == 1
            assert not worker.done()
            assert tracker.snapshot("ses-a", "thread-a").residency == "resident"
            assert tracker.snapshot("ses-a", "thread-a").cold_eligible is True
            assert scopes[0].state == "open"
            assert "模拟资源 owner 暂时不可达" in caplog.text
            assert "thread_id=thread-a" in caplog.text

            await residency_service.start()
            assert residency_service._sweep_task is worker, "重入不能创建第二个扫描 worker"
            permits.put_nowait(None)
            assert await asyncio.wait_for(ticks.get(), timeout=5) == 60
            assert attempts == 2
            assert scopes[0].state == "closed"
            assert tracker.snapshot("ses-a", "thread-a").residency == "cold"
    finally:
        await residency_service.shutdown()
    assert worker is not None and worker.cancelled()
    assert residency_service._sweep_task is None


@pytest.fixture
def held_residency_scan(
    residency_service: AgentExecutionService, monkeypatch: pytest.MonkeyPatch,
) -> tuple[asyncio.Event, asyncio.Event, list[Exception], list[int]]:
    entered, finish, wake = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failures: list[Exception] = []
    calls: list[int] = []
    sleep = asyncio.sleep

    async def controlled_sleep(delay: float) -> None:
        if delay == 60 and asyncio.current_task() is residency_service._sweep_task:
            await wake.wait()
            wake.clear()
        else:
            await sleep(delay)

    async def settle() -> None:
        await finish.wait()
        if failures:
            raise failures[0]

    async def scan() -> None:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return
        entered.set()
        await run_cleanup_shielded(settle)

    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(residency_service._residency_tracker, "sweep", scan)
    wake.set()
    return entered, finish, failures, calls


@pytest.mark.parametrize("late_failure", [False, True])
async def test_shutdown_observer_cancel_keeps_original_scan_and_delivers_late_result(
    residency_service: AgentExecutionService,
    held_residency_scan: tuple[asyncio.Event, asyncio.Event, list[Exception], list[int]],
    late_failure: bool,
) -> None:
    service = residency_service
    entered, finish, failures, calls = held_residency_scan
    failure = TimeoutError("原驻留扫描的迟到失败")
    if late_failure:
        failures.append(failure)
    await service.start()
    worker = service._sweep_task
    assert worker is not None
    await asyncio.wait_for(entered.wait(), 3)
    service._tool_face_cache[("retained",)] = []
    observer = asyncio.create_task(service.shutdown())
    try:
        async with asyncio.timeout(3):
            while not worker.cancelling():
                await asyncio.sleep(0)
        assert service._sweep_task is worker, "扫描结算前不能丢失原句柄"
        closing = service._shutdown_task
        assert closing is not None and not closing.done()
        observer.cancel("取消驻留关闭观察")
        done, _pending = await asyncio.wait((observer,), timeout=1)
        assert done == {observer}, "观察者取消不能等待或取消原关闭"
        with pytest.raises(asyncio.CancelledError, match="取消驻留关闭观察"):
            await observer
        assert not closing.done() and not worker.done() and worker.cancelling() == 1
        assert ("retained",) in service._tool_face_cache
        assert not service._residency_tracker._closing
        with pytest.raises(RuntimeError, match="正在关闭"):
            await service.start()
        finish.set()
        await asyncio.wait((closing,), timeout=3)
        assert closing.done() and calls == [1, 2] and service._sweep_task is None
        if late_failure:
            with pytest.raises(ExceptionGroup) as caught:
                await service.shutdown()
            assert caught.value.exceptions == (failure,)
            assert worker.exception() is failure
            # 只有失败交付后才允许显式重试，不复活 worker 或重发扫描。
            await service.shutdown()
            assert service._shutdown_task is not closing
        else:
            await service.shutdown()
            assert service._shutdown_task is closing
        assert not service._tool_face_cache and service._residency_tracker._closing
        await service.shutdown()
        assert calls == [1, 2]
    finally:
        finish.set()
        await asyncio.wait((worker,), timeout=3)
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, observer, return_exceptions=True)
        await asyncio.gather(service.shutdown(), return_exceptions=True)


@pytest.mark.parametrize("phase", ["scan", "scope"])
async def test_shutdown_deadline_keeps_one_owner_until_scan_or_scope_settles(
    residency_service: AgentExecutionService,
    held_residency_scan: tuple[asyncio.Event, asyncio.Event, list[Exception], list[int]],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    from app.services.orchestration import agent_execution_service as module

    service = residency_service
    entered, finish, _failures, calls = held_residency_scan
    scope_entered, scope_finish = asyncio.Event(), asyncio.Event()
    scopes: list[LifetimeScope] = []

    async def release() -> None:
        scope_entered.set()
        await scope_finish.wait()

    def build(session_id: str, thread_id: str, lifetime_scope: LifetimeScope) -> object:
        scopes.append(lifetime_scope)
        lifetime_scope.register(release, label="原 generation scope")
        return object()

    if phase == "scope":
        lease = await service._residency_tracker.acquire(
            MutationIntentOwner("session", "thread"), ExecutionAdmission(
                session_id="session", thread_id="thread", execution_id="execution",
                job_id="job", admission_idempotency_key="admission",
                execution_origin="user_turn", turn_id="turn",
            ), "v1", build,
        )
        lease.release()
        finish.set()
    await service.start()
    worker = service._sweep_task
    assert worker is not None
    await asyncio.wait_for(entered.wait(), 3)
    monkeypatch.setattr(module, "RUNTIME_SHUTDOWN_WAIT_SECONDS", 0.02)
    try:
        with pytest.raises(TimeoutError, match="ThreadRuntime关闭等待超过"):
            await service.shutdown()
        closing = service._shutdown_task
        assert closing is not None and not closing.done()
        if phase == "scan":
            assert service._sweep_task is worker and not worker.done()
        else:
            assert scope_entered.is_set() and scopes[0].state == "closing"
            assert service._sweep_task is None and service._residency_tracker._closing
        cancellations = worker.cancelling()
        with pytest.raises(TimeoutError, match="ThreadRuntime关闭等待超过"):
            await service.shutdown()
        assert service._shutdown_task is closing and worker.cancelling() == cancellations
        with pytest.raises(RuntimeError, match="正在关闭"):
            service._check_sweeper()
        finish.set()
        scope_finish.set()
        await asyncio.wait((closing,), timeout=3)
        await service.shutdown()
        assert service._shutdown_task is closing and calls == [1, 2]
        if scopes:
            assert scopes[0].is_closed
    finally:
        finish.set()
        scope_finish.set()
        await asyncio.wait((worker,), timeout=3)
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, service.shutdown(), return_exceptions=True)
