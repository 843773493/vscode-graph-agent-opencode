"""原资源观察先保存owner记录，再在线程内同步唯一账本。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from threading import get_ident

import pytest

from app.abstractions.workspace_resources import ResourceSnapshot
from app.services.business.terminal_resources.operations import TerminalOperations
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_resource_operation_close_deadline import (
    advance_clock as advance_clock,  # noqa: PLC0414
)
from tests.unit.services.business.test_resource_operation_close_deadline import (
    bundle as bundle,  # noqa: PLC0414
)
from tests.unit.services.business.workspace_resources.test_operation_release_io import (
    LeasedOperation,
    ReleaseWrite,
)
from tests.unit.services.business.workspace_resources.test_operation_release_io import (
    leased_operation as leased_operation,  # noqa: PLC0414
)
from tests.unit.services.business.workspace_resources.test_operation_release_io import (
    release_write as release_write,  # noqa: PLC0414
)


@dataclass
class ProjectionCall:
    bundle: TerminalOwnerFixture | BrowserOwnerFixture
    operation: LeasedOperation
    entry: str
    stopped: str

    async def run(self) -> ResourceSnapshot:
        operation, rid, _ = self.operation
        owner = self.bundle.owner
        assert owner is not None
        if self.entry == "detail":
            return await owner.get_resource(rid)
        if self.entry == "list":
            return next(item for item in await owner.list_resources() if item["resource_id"] == rid)
        if isinstance(operation, TerminalOperations):
            result = (await operation.read_terminal(rid))["terminal"]
        else:
            result = (await operation.perform(rid, "read", {}))["browser"]
        assert isinstance(result, dict)
        return result


@pytest.fixture(params=["tool", "detail", "list"])
def projection_call(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, leased_operation: LeasedOperation,
    request: pytest.FixtureRequest,
) -> ProjectionCall:
    stopped = "terminated" if isinstance(bundle, TerminalOwnerFixture) else "closed"
    bundle.transport.records[leased_operation[1]]["status"] = stopped
    return ProjectionCall(bundle, leased_operation, request.param, stopped)


@pytest.mark.parametrize("write_fails", [False, True])
async def test_original_observation_persists_owner_before_nonblocking_ledger_publication(
    projection_call: ProjectionCall, release_write: ReleaseWrite, write_fails: bool,
) -> None:
    case, gate = projection_call, release_write
    operation, rid, lease = case.operation
    bundle, owner = case.bundle, case.bundle.owner
    path = bundle.leases._state_path
    assert owner is not None and path is not None
    before = path.read_bytes()
    gate.failure = OSError(28, "原资源账本投影写盘失败", str(path)) if write_fails else None
    pending = asyncio.create_task(case.run())
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not pending.done(), "原账本投影写盘不能阻塞事件循环"
        assert len(gate.threads) == 1 and gate.threads[0] != get_ident()
        assert owner._resource_lock(rid).locked() and path.read_bytes() == before
        record = type(owner._store)(workspace_root=bundle.root, workspace_id=owner.workspace_id, kind=owner.kind).get(rid)
        assert record is not None and record.status == case.stopped and record.reconciliation_status == "verified"
        prior = type(bundle.leases)(state_path=path).get(rid)
        assert prior is not None and prior.status == "running"
        assert bundle.leases.get_lease(lease.lease_id) == lease
        gate.release.set()
        if write_fails:
            with pytest.raises(OSError) as caught:
                await pending
            assert caught.value.__cause__ is gate.failure and caught.value.errno == 28
            assert str(path) not in str(caught.value)
            if case.entry == "tool":
                failed = owner._store.get(rid)
                protected = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
                assert failed is not None and failed.reconciliation_status == "unknown"
                assert protected is not None and protected.status == "reconcile_required"
                result = await bundle.command(rid, action="stop", expected_revision=record.owner_revision)
            else:
                assert path.read_bytes() == before
                result = await case.run()
        else:
            result = await pending
        assert result["resource_id"] == rid and result["status"] == case.stopped
        assert result["reconciliation_status"] == "verified"
        stopped = type(bundle.leases)(state_path=path).get(rid)
        assert stopped is not None and stopped.status == "stopped"
        await operation.close()
        saved = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
        assert saved is not None and saved.status == ("settled" if write_fails and case.entry == "tool" else "released")
        assert saved.resource_id == rid and saved.operation_id == lease.operation_id
        assert bundle.transport.creates == 1 and bundle.transport.deleted == []
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("write_fails", [False, True])
async def test_cancelled_observation_settles_original_writer_before_unlocking(
    projection_call: ProjectionCall, release_write: ReleaseWrite, write_fails: bool,
) -> None:
    case, gate = projection_call, release_write
    operation, rid, lease = case.operation
    bundle, owner = case.bundle, case.bundle.owner
    path = bundle.leases._state_path
    assert owner is not None and path is not None
    gate.failure = OSError(28, "取消期间投影写盘失败", str(path)) if write_fails else None
    pending = asyncio.create_task(case.run())
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not pending.done(), "原投影写盘期间必须仍能观察取消"
        pending.cancel("原投影取消")
        await asyncio.sleep(0)
        pending.cancel("重复取消不能提前释放原资源锁")
        await asyncio.sleep(0)
        assert not pending.done() and owner._resource_lock(rid).locked()
        assert bundle.leases.get_lease(lease.lease_id) == lease
        gate.release.set()
        with pytest.raises(asyncio.CancelledError, match="原投影取消") as caught:
            await pending
        if write_fails:
            failure = caught.value.__cause__
            assert isinstance(failure, OSError) and failure.__cause__ is gate.failure
        assert not owner._resource_lock(rid).locked()
        ledger = type(bundle.leases)(state_path=path)
        saved = ledger.get(rid)
        expected = "unknown" if case.entry == "tool" else "running" if write_fails else "stopped"
        assert saved is not None and saved.status == expected
        protected = ledger.get_lease(lease.lease_id)
        assert protected is not None and protected.status == ("reconcile_required" if case.entry == "tool" else "active")
        await operation.close()
        released = bundle.leases.get_lease(lease.lease_id)
        assert released is not None and released.status == ("reconcile_required" if case.entry == "tool" else "released")
        assert bundle.transport.creates == 1 and bundle.transport.deleted == []
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("projection_call", ["detail", "list"], indirect=True)
@pytest.mark.parametrize("write_fails", [False, True])
async def test_observation_deadline_waits_for_original_commit_without_new_unknown(
    projection_call: ProjectionCall, release_write: ReleaseWrite,
    advance_clock: Callable[[float], None], write_fails: bool,
) -> None:
    case, gate = projection_call, release_write
    operation, rid, lease = case.operation
    bundle, owner = case.bundle, case.bundle.owner
    path = bundle.leases._state_path
    assert owner is not None and path is not None
    gate.failure = OSError(28, "到期期间投影写盘失败", str(path)) if write_fails else None
    pending = asyncio.create_task(case.run())
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        assert not pending.done(), "同步投影不能阻塞原观察期限"
        advance_clock(31)
        for _ in range(5):
            await asyncio.sleep(0)
        assert not pending.done() and owner._resource_lock(rid).locked()
        gate.release.set()
        with pytest.raises(TimeoutError, match="等待超过 30 秒") as caught:
            await pending
        if write_fails:
            budget_error = caught.value.__cause__
            assert isinstance(budget_error, TimeoutError)
            cancelled = budget_error.__cause__
            assert isinstance(cancelled, asyncio.CancelledError)
            failure = cancelled.__cause__
            assert isinstance(failure, OSError) and failure.__cause__ is gate.failure
        record = owner._store.get(rid)
        assert record is not None and record.reconciliation_status == "verified" and record.status == case.stopped
        cold = type(bundle.leases)(state_path=path)
        stopped = cold.get(rid)
        assert stopped is not None and stopped.status == ("running" if write_fails else "stopped")
        assert cold.get_lease(lease.lease_id) == lease
        assert (await case.run())["status"] == case.stopped
        await operation.close()
        released = bundle.leases.get_lease(lease.lease_id)
        assert released is not None and released.status == "released"
        assert bundle.transport.creates == 1 and bundle.transport.deleted == []
    finally:
        gate.release.set()
        await asyncio.gather(pending, return_exceptions=True)
