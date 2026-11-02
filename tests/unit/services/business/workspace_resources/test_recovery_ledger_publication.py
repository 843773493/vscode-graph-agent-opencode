"""原版本同步成功后，取消必须等同一账本发布及pending结算。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import get_ident

import pytest

from app.services.business.workspace_resources import owner as owner_module
from app.services.business.workspace_resources.owner import WorkspaceResourceOwner
from app.services.business.workspace_resources.records import (
    ForkAttachReceipt,
    ThreadRef,
)
from app.services.business.workspace_resources.recovery import RecoveryVersion
from app.services.infrastructure.external_resource_leases import ResourceLease
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_B, THREAD_B
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_resource_operation_close_deadline import (
    advance_clock as advance_clock,  # noqa: PLC0414
)
from tests.unit.services.business.test_resource_operation_close_deadline import (
    bundle as bundle,  # noqa: PLC0414
)
from tests.unit.services.business.workspace_resources.test_operation_release_io import (
    ReleaseWrite,
)


@dataclass
class ColdPublication:
    owner: WorkspaceResourceOwner
    bundle: TerminalOwnerFixture | BrowserOwnerFixture
    rid: str
    revision: int
    gate: ReleaseWrite
    calls: list[tuple[str, int]]


@pytest.fixture
async def cold_publication(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ColdPublication]:
    rid = str((await bundle.create("workspace"))["resource_id"])
    owner = bundle.restart()
    await owner.recover(())
    assert owner._recovery._pending is not None
    revision = owner._recovery._pending[rid].revision
    assert revision is not None
    calls: list[tuple[str, int]] = []
    sync = bundle.transport.sync_owner_revision

    async def synced(resource_id: str, version: int, *, for_stop: bool) -> None:
        calls.append((resource_id, version))
        await sync(resource_id, version, for_stop=for_stop)

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", synced)
    path = bundle.leases._state_path
    assert path is not None
    gate, loop, replace = ReleaseWrite(), asyncio.get_running_loop(), Path.replace

    def publishing(source: Path, target: Path) -> Path:
        if target == path and not gate.entered.is_set():
            loop.call_soon_threadsafe(gate.entered.set)
            if not gate.release.wait(3):
                raise TimeoutError("测试未放行原恢复账本写入")
            if gate.failure is not None:
                raise gate.failure
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", publishing)
    try:
        yield ColdPublication(owner, bundle, rid, revision, gate, calls)
    finally:
        gate.release.set()


@pytest.mark.parametrize("ending", ["cancel", "deadline"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_cold_recovery_settles_original_publication_before_cancelling(
    cold_publication: ColdPublication, advance_clock: Callable[[float], None],
    ending: str, write_fails: bool,
) -> None:
    case = cold_publication
    owner, bundle, rid, gate = case.owner, case.bundle, case.rid, case.gate
    path = bundle.leases._state_path
    assert path is not None
    before = path.read_bytes()
    gate.failure = OSError(28, "原冷恢复账本发布失败", str(path)) if write_fails else None
    pending = asyncio.create_task(owner.recover((rid,)))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not pending.done(), "原恢复发布必须允许事件循环处理取消"
        assert case.calls == [(rid, case.revision)] and owner._resource_lock(rid).locked()
        assert owner._recovery._pending == {rid: RecoveryVersion(case.revision)}
        assert path.read_bytes() == before
        if ending == "cancel":
            pending.cancel("原恢复发布取消")
        else:
            advance_clock(31)
        for _ in range(5):
            await asyncio.sleep(0)
        if ending == "cancel":
            pending.cancel("重复取消不能遗弃原恢复提交")
            await asyncio.sleep(0)
        assert not pending.done() and owner._resource_lock(rid).locked()
        gate.release.set()
        with pytest.raises(asyncio.CancelledError if ending == "cancel" else TimeoutError) as caught:
            await pending
        if ending == "cancel":
            cancellation = caught.value
            assert cancellation.args == ("原恢复发布取消",)
        else:
            budget = caught.value.__cause__
            assert isinstance(budget, TimeoutError)
            cancellation = budget.__cause__
        assert isinstance(cancellation, asyncio.CancelledError)
        if write_fails:
            failure = cancellation.__cause__
            assert isinstance(failure, OSError) and failure.__cause__ is gate.failure
            assert owner._recovery._pending == {rid: RecoveryVersion(case.revision)}
            assert path.read_bytes() == before
        else:
            assert owner._recovery._pending == {}
        assert not owner._resource_lock(rid).locked()
        await owner.recover((rid,))
        assert owner._recovery._pending == {}
        assert case.calls == [(rid, case.revision)] * (2 if write_fails else 1)
        record = owner._store.get(rid)
        external = type(bundle.leases)(state_path=path).get(rid)
        assert record is not None and record.owner_revision == case.revision
        assert record.reconciliation_status == "verified"
        assert external is not None and external.status == "running"
        assert bundle.transport.creates == 1 and bundle.transport.deleted == []
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)


@dataclass
class ColdPreparation:
    owner: WorkspaceResourceOwner
    bundle: TerminalOwnerFixture | BrowserOwnerFixture
    rid: str
    revision: int
    gate: ReleaseWrite
    calls: list[tuple[str, int]]
    writers: list[int]
    receipt: ForkAttachReceipt | None
    lease: ResourceLease | None


@pytest.fixture(params=["reconcile", "fork-receipt"])
async def cold_preparation(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[ColdPreparation]:
    rid = str((await bundle.create("workspace"))["resource_id"])
    owner = bundle.owner
    assert owner is not None
    committed_phase = f"fork.live.committed.{owner.kind}"
    receipt, lease = None, None
    if request.param == "fork-receipt":
        original = asyncio.CancelledError("原fork已提交但确认中断")
        failure = OSError(28, "原fork回执确认未落盘")
        observe, finish = owner_module.observe_phase, bundle.leases.finish_operation_dispatch

        async def committed(operation_id: str, phase: str, *, details: dict[str, object] | None = None) -> None:
            if operation_id == "cold-preparation-fork" and phase == committed_phase:
                raise original
            await observe(operation_id, phase, details=details)

        def unconfirmed(lease_id: str, *, release: bool = False) -> None:
            if release:
                raise failure
            finish(lease_id, release=release)

        with monkeypatch.context() as fault:
            fault.setattr(owner_module, "observe_phase", committed)
            fault.setattr(bundle.leases, "finish_operation_dispatch", unconfirmed)
            with pytest.raises(asyncio.CancelledError) as caught:
                await owner.attach_for_fork(rid, creation_key="cold-preparation-fork", expected_revision=1,
                                            thread=ThreadRef(SESSION_B, THREAD_B))
            assert caught.value is original and caught.value.__cause__ is failure
        receipt = owner.fork_attach_receipt(rid, creation_key="cold-preparation-fork", expected_revision=1,
                                             thread=ThreadRef(SESSION_B, THREAD_B))
        assert receipt is not None and receipt.state == "committed" and receipt.lease_id is not None
        lease = bundle.leases.get_lease(receipt.lease_id)
        assert lease is not None and lease.status == "dispatching"
    record = owner._store.get(rid)
    assert record is not None
    revision = record.owner_revision + 1
    owner = bundle.restart()
    calls: list[tuple[str, int]] = []
    sync = bundle.transport.sync_owner_revision

    async def synced(resource_id: str, version: int, *, for_stop: bool) -> None:
        calls.append((resource_id, version))
        await sync(resource_id, version, for_stop=for_stop)

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", synced)
    path = bundle.leases._state_path
    assert path is not None
    gate, loop, publishing = ReleaseWrite(), asyncio.get_running_loop(), Path.replace
    writers: list[int] = []
    armed = True

    def publish(source: Path, target: Path) -> Path:
        nonlocal armed
        if target == path:
            writers.append(get_ident())
            candidate = json.loads(source.read_text())
            selected = request.param == "reconcile" or any(
                item["lease_id"] == (lease.lease_id if lease is not None else None) and item["status"] == "released"
                for item in candidate["leases"]
            )
            if armed and selected:
                armed = False
                loop.call_soon_threadsafe(gate.entered.set)
                if not gate.release.wait(5):
                    raise TimeoutError("测试未放行冷准备的原生账本写入")
                if gate.failure is not None:
                    raise gate.failure
        return publishing(source, target)

    monkeypatch.setattr(Path, "replace", publish)
    try:
        yield ColdPreparation(owner, bundle, rid, revision, gate, calls, writers, receipt, lease)
    finally:
        gate.release.set()


@pytest.mark.parametrize("ending", ["complete", "cancel"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_native_preparation_preserves_loop_original_cancellation_and_frozen_fork_receipt(
    cold_preparation: ColdPreparation, ending: str, write_fails: bool,
) -> None:
    case = cold_preparation
    owner, bundle, rid, gate = case.owner, case.bundle, case.rid, case.gate
    path = bundle.leases._state_path
    assert path is not None
    gate.failure = OSError(28, "冷准备原生发布失败", str(path)) if write_fails else None
    pending = asyncio.create_task(owner.recover((rid,)))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not pending.done() and owner._recovery._prepare_lock.locked()
        assert owner._recovery._pending is None and case.calls == []
        assert case.writers and all(writer != get_ident() for writer in case.writers)
        before = path.read_bytes()
        if ending == "cancel":
            pending.cancel("原冷准备取消")
            for _ in range(5):
                await asyncio.sleep(0)
            pending.cancel("重复取消不得遗弃冷准备")
            await asyncio.sleep(0)
            assert not pending.done() and owner._recovery._prepare_lock.locked()
            assert path.read_bytes() == before and case.calls == []
        gate.release.set()
        if ending == "cancel":
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await pending
            assert cancelled.value.args == ("原冷准备取消",)
            failure = cancelled.value.__cause__
        elif write_fails:
            with pytest.raises(OSError) as failed:
                await pending
            failure = failed.value
        else:
            await pending
            failure = None
        if write_fails:
            assert isinstance(failure, OSError) and failure.__cause__ is gate.failure
            assert owner._recovery._pending is None and path.read_bytes() == before
        else:
            assert failure is None
            assert owner._recovery._pending == ({rid: RecoveryVersion(case.revision)} if ending == "cancel" else {})
        assert not owner._recovery._prepare_lock.locked()
        await owner.recover((rid,))
        assert owner._recovery._pending == {} and case.calls == [(rid, case.revision)]
        current = owner._store.get(rid)
        assert current is not None and current.owner_revision == case.revision and current.reconciliation_status == "verified"
        assert bundle.transport.creates == 1 and bundle.transport.deleted == []
        if case.receipt is not None:
            assert case.lease is not None
            result, receipt = await owner.attach_for_fork(
                rid, creation_key="cold-preparation-fork", expected_revision=1, thread=ThreadRef(SESSION_B, THREAD_B),
            )
            assert receipt == case.receipt and result["owner_revision"] == case.revision
            assert current.thread_associations.count(ThreadRef(SESSION_B, THREAD_B)) == 1
            released = type(bundle.leases)(state_path=path).get_lease(case.lease.lease_id)
            assert released is not None and released.status == "released"
            assert released == replace(case.lease, status="released", updated_at=released.updated_at)
        assert case.calls == [(rid, case.revision)]
        assert all(writer != get_ident() for writer in case.writers)
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_cold_preparation_reuses_registered_identities_and_commits_one_reconciliation(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = [str((await bundle.create("workspace"))["resource_id"]) for _ in range(4)]
    leases = [bundle.leases.acquire_operation(resource_id=rid, turn_stream_id="old-holder", operation_id="old-operation")
              for rid in resources]
    bundle.leases.mark_operation_dispatched(leases[0].lease_id)
    before = {rid: bundle.leases.get(rid) for rid in resources}
    owner = bundle.restart()
    path = bundle.leases._state_path
    assert path is not None
    publishing = Path.replace
    writes: list[dict[str, object]] = []

    def publish(source: Path, target: Path) -> Path:
        if target == path:
            writes.append(json.loads(source.read_text()))
        return publishing(source, target)

    with monkeypatch.context() as observe:
        observe.setattr(Path, "replace", publish)
        await owner.recover(())
    assert len(writes) == 1, "冷准备只发布整批对账，不逐资源重写已登记身份"
    assert owner._recovery._pending is not None and set(owner._recovery._pending) == set(resources)
    cold = type(bundle.leases)(state_path=path)
    for rid, lease in zip(resources, leases, strict=True):
        record, previous, retained = cold.get(rid), before[rid], cold.get_lease(lease.lease_id)
        assert record is not None and previous is not None and retained is not None
        assert record == replace(previous, status="recovered", updated_at=record.updated_at)
        assert retained == replace(lease, status="reconcile_required", updated_at=retained.updated_at)
    await owner.recover(resources)
    assert owner._recovery._pending == {} and bundle.transport.creates == len(resources)
    assert bundle.transport.deleted == []
    for rid in resources:
        record = owner._store.get(rid)
        assert record is not None and record.owner_revision == 2 and record.reconciliation_status == "reconcile_required"
