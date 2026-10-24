"""闲置 worker 的真实 scope 清理、失败重试与关闭行为。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.core.lifecycle import LifetimeScope
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

    def build(session_id: str, thread_id: str, scope: LifetimeScope) -> object:
        scopes.append(scope)
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
