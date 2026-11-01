"""claim 提交期间的真实存储、卸载准入及取消结算。"""

from __future__ import annotations

import asyncio
import errno
import os
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, get_ident

import pytest

from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.process.claim_runtime import (
    NodeDebugClaimRuntime,
)
from app.services.infrastructure.node_debug.process.launch_claim import claim_marked
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from app.services.infrastructure.node_debug.session.thread_owner import (
    SessionNodePathResolver,
)
from app.services.orchestration.thread_residency import ThreadResidencyTracker
from tests.unit.services.orchestration.test_thread_residency import (
    _OWNER,
    _FakeMonotonicClock,
    _launch_pending_claim,
    _resident_runtime,
)
from tests.unit.services.orchestration.test_thread_residency import (
    session_tree as session_tree,  # noqa: PLC0414 - 复用原 catalog 装具
)


@dataclass
class ClaimPublication:
    store: NodeDebugSessionStore
    claims: NodeDebugClaimRuntime
    tracker: ThreadResidencyTracker
    clock: _FakeMonotonicClock
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: Event = field(default_factory=Event)
    failure: OSError | None = None
    writers: list[int] = field(default_factory=list)
    armed: bool = True


@pytest.fixture
async def claim_publication(
    session_tree: SessionNodePathResolver, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ClaimPublication]:
    clock = _FakeMonotonicClock()
    tracker = ThreadResidencyTracker(clock=clock)
    store = NodeDebugSessionStore(session_tree)
    claims = NodeDebugClaimRuntime(
        session_store=store, external_resource_leases=ExternalResourceLeaseLedger(),
        runtimes={}, residency_tracker=tracker, state_events=None,
    )
    tracker.add_blocker_source(claims)
    gate = ClaimPublication(store, claims, tracker, clock)
    loop, replace = asyncio.get_running_loop(), os.replace

    def publish(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(target).name == NodeDebugSessionStore.LAUNCH_CLAIM_FILE_NAME and gate.armed:
            gate.armed = False
            gate.writers.append(get_ident())
            loop.call_soon_threadsafe(gate.entered.set)
            assert gate.release.wait(4), "测试未放行原 claim 提交"
            if gate.failure is not None:
                raise gate.failure
        replace(source, target)

    monkeypatch.setattr(os, "replace", publish)
    try:
        yield gate
    finally:
        gate.release.set()
        await tracker.close()


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel", "cancel-failure"])
async def test_pending_claim_pins_idle_runtime_until_original_commit_settles(
    claim_publication: ClaimPublication, outcome: str,
) -> None:
    gate = claim_publication
    lease = await _resident_runtime(gate.tracker)
    gate.clock.advance(1800)
    if "failure" in outcome:
        gate.failure = OSError(errno.ENOSPC, "claim 提交失败")
    claim = _launch_pending_claim()
    pending = asyncio.create_task(gate.claims.write_launch_claim(claim))
    try:
        await asyncio.wait_for(gate.entered.wait(), 6)
        assert len(gate.writers) == 1 and gate.writers[0] != get_ident()
        assert gate.store.read_launch_claim(*_OWNER) is None
        await gate.tracker.sweep()
        assert gate.tracker.is_current_generation(*_OWNER, lease.generation)
        assert gate.tracker.snapshot(*_OWNER).blockers
        if outcome.startswith("cancel"):
            pending.cancel("原 claim 提交取消")
            await asyncio.sleep(0)
            pending.cancel("重复取消")
            await asyncio.sleep(0)
            assert not pending.done()
        gate.release.set()
        if outcome.startswith("cancel"):
            with pytest.raises(asyncio.CancelledError, match="原 claim 提交取消") as caught:
                await pending
            if gate.failure is not None:
                assert isinstance(caught.value.__cause__, OSError)
                assert caught.value.__cause__.__cause__ is gate.failure
        elif gate.failure is not None:
            with pytest.raises(OSError) as caught:
                await pending
            assert caught.value.__cause__ is gate.failure
        else:
            await pending
        published = gate.store.read_launch_claim(*_OWNER)
        assert (published is None) == (gate.failure is not None)
        pushed = gate.tracker._states[_OWNER].blockers
        assert not any(key.startswith("node_debug_claim_write:") for key in pushed)
        if published is not None:
            assert published.process_instance_id == claim.process_instance_id
            assert f"node_debug_claim:{claim.process_instance_id}" in pushed
            await gate.claims.write_launch_claim(claim_marked(published, phase="settled"))
        assert gate.tracker.snapshot(*_OWNER).blockers == ()
        gate.clock.advance(1800)
        await gate.tracker.sweep()
        assert not gate.tracker.is_current_generation(*_OWNER, lease.generation)
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_failed_settled_claim_keeps_previous_durable_blocker(
    claim_publication: ClaimPublication,
) -> None:
    gate = claim_publication
    gate.armed = False
    claim = _launch_pending_claim()
    await gate.claims.write_launch_claim(claim)
    before = gate.store.read_launch_claim(*_OWNER)
    gate.armed = True
    gate.failure = OSError(errno.EIO, "终态未提交")
    pending = asyncio.create_task(gate.claims.write_launch_claim(claim_marked(claim, phase="settled")))
    try:
        await asyncio.wait_for(gate.entered.wait(), 6)
        assert gate.writers[0] != get_ident()
        assert gate.tracker.snapshot(*_OWNER).blockers
        gate.release.set()
        with pytest.raises(OSError) as caught:
            await pending
        assert caught.value.__cause__ is gate.failure
        assert gate.store.read_launch_claim(*_OWNER) == before
        assert gate.tracker.snapshot(*_OWNER).blockers
        await gate.claims.write_launch_claim(claim_marked(claim, phase="settled"))
        assert gate.tracker.snapshot(*_OWNER).blockers == ()
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("fail_write", [False, True], ids=["commit", "late-failure"])
async def test_cancelled_claim_holds_admission_while_waiting_in_real_executor_queue(
    claim_publication: ClaimPublication, fail_write: bool,
) -> None:
    gate = claim_publication
    lease = await _resident_runtime(gate.tracker)
    gate.clock.advance(1800)
    loop = asyncio.get_running_loop()
    occupied = asyncio.Event()
    free_worker = Event()
    # pytest 的每个用例独立 event loop；此真实单 worker 随该 loop 一起关闭。
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))

    def occupy_worker() -> None:
        loop.call_soon_threadsafe(occupied.set)
        assert free_worker.wait(4), "测试未归还原线程池 worker"

    busy = loop.run_in_executor(None, occupy_worker)
    await asyncio.wait_for(occupied.wait(), 2)
    if fail_write:
        gate.failure = OSError(errno.ENOSPC, "线程池排队后的原写入失败")
    pending = asyncio.create_task(gate.claims.write_launch_claim(_launch_pending_claim()))
    try:
        async with asyncio.timeout(2):
            while not gate.tracker.snapshot(*_OWNER).blockers:
                await asyncio.sleep(0)
        assert not gate.entered.is_set() and not pending.done()
        pending.cancel("线程池排队期间的原取消")
        await asyncio.sleep(0)
        pending.cancel("重复取消")
        await asyncio.sleep(0)
        await gate.tracker.sweep()
        assert gate.tracker.is_current_generation(*_OWNER, lease.generation)
        assert not pending.done() and gate.store.read_launch_claim(*_OWNER) is None
        free_worker.set()
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not pending.done() and gate.writers[0] != get_ident()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="线程池排队期间的原取消") as caught:
            await pending
        if gate.failure is not None:
            assert isinstance(caught.value.__cause__, OSError)
            assert caught.value.__cause__.__cause__ is gate.failure
        published = gate.store.read_launch_claim(*_OWNER)
        assert (published is None) == fail_write
        if published is not None:
            await gate.claims.write_launch_claim(claim_marked(published, phase="settled"))
        assert gate.tracker.snapshot(*_OWNER).blockers == ()
    finally:
        free_worker.set()
        gate.release.set()
        await asyncio.gather(busy, pending, return_exceptions=True)
