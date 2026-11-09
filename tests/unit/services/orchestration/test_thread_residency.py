"""ThreadResidency 基础设施与 Debug run blocker 的单元测试。

覆盖 R5b（OpenSpec ``add-context-injection-lifecycle`` 任务 2.8 与
``add-itemized-rollout-context`` 任务 8.8/8.8-A 的 residency 切片 + R3b 复核残留项）：

- fake clock 29:59 仍 resident / 30:00 无 blocker → cold-eligible（关闭真实 generation scope）；
  产品阈值常量 1800 秒不缩短；
- blocker 活跃期间跨阈值仍 resident（idle 不累计），解除后从解除时刻重新起算；
- ``record_activity`` 重置累计；
- scope 关闭边界：generation 令牌、无 generation 不卸载、失败 fail-loud 且可重试；
- 旧 Node Debug Backend spawn 状态机的独立验证已迁移到插件纵向测试。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.lifecycle import LifetimeScope, ReleaseCallback
from app.core.path_utils import get_session_path_resolver
from app.core.thread_residency_contracts import ResidencyBlocker
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.orchestration.thread_residency import (
    THREAD_IDLE_UNLOAD_SECONDS,
    ThreadResidencyTracker,
    ThreadRuntimeLease,
)
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

_PARENT_SESSION_ID = "ses_00000000400070008000000000000001"
_THREAD_ID = "thr_00000000400070008000000000000002"
_OWNER = (_PARENT_SESSION_ID, _THREAD_ID)


# ---------------------------------------------------------------------------
# 时钟替身与基础 fixture
# ---------------------------------------------------------------------------


class _FakeMonotonicClock:
    """可推进的单调时钟替身：idle 判定唯一依据，绝不使用墙钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FixedWallClock:
    """固定墙钟：只用于快照展示字段的确定性断言。"""

    def __init__(self) -> None:
        self.moment = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.moment


def _create_session(sessions_root: Path, session_id: str) -> Path:
    """在权威目录索引中创建最小合法会话节点。"""
    title = f"测试会话 {session_id}"
    return seed_catalog_session_bundle(
        sessions_root,
        session_id,
        title=title,
        main_thread_id=_THREAD_ID if session_id == _PARENT_SESSION_ID else None,
    ).directory


@pytest.fixture
def session_tree(tmp_path: Path):
    sessions_root = tmp_path / ".boxteam" / "sessions"
    resolver = get_session_path_resolver(sessions_root)
    resolver.initialize()
    _create_session(sessions_root, _PARENT_SESSION_ID)
    return resolver


# ---------------------------------------------------------------------------
# Part 1：tracker 纯单元（fake clock 29:59 / 30:00 边界、blocker、回调缝）
# ---------------------------------------------------------------------------


def _make_tracker(
    clock: _FakeMonotonicClock,
    *,
    wall_clock: _FixedWallClock | None = None,
) -> ThreadResidencyTracker:
    return ThreadResidencyTracker(
        clock=clock, wall_clock=wall_clock or _FixedWallClock()
    )


def _unit_admission() -> ExecutionAdmission:
    return ExecutionAdmission(
        session_id=_OWNER[0],
        thread_id=_OWNER[1],
        execution_id="execution_unit",
        job_id="job_unit",
        admission_idempotency_key="admission_unit",
        execution_origin="user_turn",
        turn_id="turn_unit",
    )


async def _resident_runtime(
    tracker: ThreadResidencyTracker,
    *,
    release: ReleaseCallback | None = None,
    generation_key: str = "config-v1",
) -> ThreadRuntimeLease[object]:
    def build(session_id: str, thread_id: str, scope: LifetimeScope) -> object:
        if release is not None:
            scope.register(release, label="测试资源")
        return object()

    lease = await tracker.acquire(
        MutationIntentOwner(*_OWNER),
        _unit_admission(),
        generation_key,
        build,
    )
    async with lease:
        lease.release()
        return lease


@pytest.mark.asyncio
async def test_fake_clock_29_59_resident_and_30_00_cold_eligible_fires_unload() -> None:
    """产品阈值不缩短；scope 只在 30:00 后真正关闭。"""
    assert THREAD_IDLE_UNLOAD_SECONDS == 1800.0
    clock = _FakeMonotonicClock()
    fired: list[str] = []
    tracker = _make_tracker(clock)
    generation = await _resident_runtime(
        tracker, release=lambda: fired.append("closed")
    )
    assert generation.generation == 1
    tracker.record_activity(*_OWNER)
    clock.advance(1799)
    assert await tracker.sweep() == ()
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.residency == "resident"
    assert snapshot.cold_eligible is False
    assert snapshot.idle_seconds == pytest.approx(1799.0)
    assert fired == []
    clock.advance(1)
    assert tracker.snapshot(*_OWNER).cold_eligible is True
    unloaded = await tracker.sweep()
    assert len(unloaded) == 1
    assert (unloaded[0].session_id, unloaded[0].thread_id) == _OWNER
    assert unloaded[0].generation == 1
    assert unloaded[0].idle_seconds == pytest.approx(1800.0)
    assert fired == ["closed"]
    after = tracker.snapshot(*_OWNER)
    assert after.residency == "cold"
    assert after.cold_eligible is False


@pytest.mark.asyncio
async def test_active_blocker_keeps_thread_resident_across_threshold() -> None:
    clock = _FakeMonotonicClock()
    fired: list[str] = []
    tracker = _make_tracker(clock)
    await _resident_runtime(tracker, release=lambda: fired.append("closed"))
    tracker.register_blocker(
        *_OWNER,
        blocker_key="node_debug_claim:proc-1",
        kind="node_debug_process",
        reason="Node 调试进程运行中",
    )
    tracker.record_activity(*_OWNER)
    clock.advance(7200)
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.residency == "resident"
    assert snapshot.cold_eligible is False
    assert snapshot.idle_seconds is None
    assert snapshot.idle_deadline is None
    assert snapshot.execution_state == "node_debug_process"
    assert await tracker.sweep() == ()
    assert fired == []


@pytest.mark.asyncio
async def test_blocker_release_restarts_idle_counting_from_release_moment() -> None:
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    await _resident_runtime(tracker)
    tracker.register_blocker(
        *_OWNER,
        blocker_key="node_debug_claim:proc-1",
        kind="node_debug_process",
        reason="Node 调试进程运行中",
    )
    clock.advance(1000)
    tracker.release_blocker(*_OWNER, blocker_key="node_debug_claim:proc-1")
    clock.advance(1799)
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.residency == "resident"
    assert snapshot.cold_eligible is False
    assert snapshot.idle_seconds == pytest.approx(1799.0)
    clock.advance(1)
    assert tracker.snapshot(*_OWNER).cold_eligible is True


@pytest.mark.asyncio
async def test_record_activity_resets_idle_accumulation() -> None:
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    await _resident_runtime(tracker)
    clock.advance(1000)
    tracker.record_activity(*_OWNER)
    clock.advance(1799)
    assert tracker.snapshot(*_OWNER).idle_seconds == pytest.approx(1799.0)
    assert tracker.snapshot(*_OWNER).residency == "resident"
    clock.advance(1)
    assert tracker.snapshot(*_OWNER).cold_eligible is True


@pytest.mark.asyncio
async def test_record_activity_during_block_does_not_predate_release_restart() -> None:
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    await _resident_runtime(tracker)

    class _StaticSource:
        blockers = (
            ResidencyBlocker(kind="node_debug_process", reason="Node 调试进程运行中"),
        )

        def residency_blockers(self, session_id: str, thread_id: str):
            return self.blockers

    source = _StaticSource()
    tracker.add_blocker_source(source)
    assert tracker.snapshot(*_OWNER).blockers
    clock.advance(600)
    tracker.record_activity(*_OWNER)
    clock.advance(600)
    source.blockers = ()
    assert tracker.snapshot(*_OWNER).blockers == ()
    clock.advance(1799)
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.residency == "resident"
    assert snapshot.idle_seconds == pytest.approx(1799.0)


@pytest.mark.asyncio
async def test_sweep_without_runtime_never_fires_unload() -> None:
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    tracker.record_activity(*_OWNER)
    clock.advance(3600)
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.generation == 0
    assert snapshot.residency == "cold"
    assert snapshot.cold_eligible is False
    assert await tracker.sweep() == ()


@pytest.mark.asyncio
async def test_unload_failure_propagates_and_thread_stays_eligible_for_retry() -> None:
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    calls = 0

    def release() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("释放该 generation 的 LifetimeScope 失败")

    await _resident_runtime(tracker, release=release)
    clock.advance(1800)
    with pytest.raises(ExceptionGroup, match="LifetimeScope"):
        await tracker.sweep()
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.cold_eligible is True
    assert snapshot.residency == "unloading"
    assert len(await tracker.sweep()) == 1
    assert tracker.snapshot(*_OWNER).residency == "cold"
    assert calls == 2


@pytest.mark.asyncio
async def test_new_generation_enables_unload_again_and_old_one_stays_unloaded() -> None:
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    closed: list[int] = []
    await _resident_runtime(tracker, release=lambda: closed.append(1))
    clock.advance(1800)
    await tracker.sweep()
    assert closed == [1]
    clock.advance(1800)
    assert tracker.snapshot(*_OWNER).cold_eligible is False
    assert await tracker.sweep() == ()
    second = await _resident_runtime(tracker, release=lambda: closed.append(2))
    assert second.generation == 2
    clock.advance(1800)
    await tracker.sweep()
    assert closed == [1, 2]


@pytest.mark.asyncio
async def test_sweep_and_admission_serialize_generation_scope_close() -> None:
    """真实 scope 尚在关闭时，新代不能绕过旧代或被旧代错标 cold。"""
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    started, release = asyncio.Event(), asyncio.Event()

    async def held_close() -> None:
        started.set()
        await release.wait()

    first = await _resident_runtime(tracker, release=held_close)
    clock.advance(1800)
    sweep = asyncio.create_task(tracker.sweep())
    await asyncio.wait_for(started.wait(), timeout=5)
    second_task = asyncio.create_task(
        _resident_runtime(tracker, generation_key="config-v2")
    )
    await asyncio.sleep(0)
    assert not second_task.done()
    assert not tracker.is_current_generation(*_OWNER, first.generation)
    release.set()
    unloaded, second = await asyncio.wait_for(
        asyncio.gather(sweep, second_task), timeout=5
    )
    assert len(unloaded) == 1
    assert second.generation == 2
    snapshot = tracker.snapshot(*_OWNER)
    assert snapshot.generation == 2
    assert snapshot.residency == "resident"
    assert snapshot.cold_eligible is False
    assert tracker.is_current_generation(*_OWNER, 2)


@pytest.mark.asyncio
async def test_concurrent_sweeps_close_inflight_generation_scope_at_most_once() -> None:
    clock = _FakeMonotonicClock()
    tracker = _make_tracker(clock)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def held_close() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    await _resident_runtime(tracker, release=held_close)
    clock.advance(1800)
    first = asyncio.create_task(tracker.sweep())
    await asyncio.wait_for(entered.wait(), timeout=5)
    second = asyncio.create_task(tracker.sweep())
    await asyncio.sleep(0)
    release.set()
    first_result, second_result = await asyncio.wait_for(
        asyncio.gather(first, second), timeout=5
    )
    assert calls == 1
    assert len(first_result) == 1
    assert second_result == ()
    assert tracker.snapshot(*_OWNER).residency == "cold"


@pytest.mark.asyncio
async def test_late_old_generation_callback_is_fenced_after_rebuild() -> None:
    tracker = _make_tracker(_FakeMonotonicClock())
    first = await _resident_runtime(tracker)
    assert tracker.is_current_generation(*_OWNER, first.generation)
    second = await _resident_runtime(tracker, generation_key="config-v2")
    writes: list[str] = []

    def old_callback() -> None:
        if tracker.is_current_generation(*_OWNER, first.generation):
            writes.append("stale write")

    old_callback()
    assert writes == []
    assert tracker.is_current_generation(*_OWNER, second.generation)
    assert not tracker.is_current_generation("ses_unknown", "main", 1)
    assert not tracker.is_current_generation(*_OWNER, 0)


def test_tracker_configuration_fails_loud() -> None:
    clock = _FakeMonotonicClock()
    with pytest.raises(ValueError, match="idle 阈值"):
        ThreadResidencyTracker(clock=clock, idle_timeout_seconds=0)
    tracker = _make_tracker(clock)
    with pytest.raises(TypeError, match="residency_blockers"):
        tracker.add_blocker_source(object())  # type: ignore[arg-type]
