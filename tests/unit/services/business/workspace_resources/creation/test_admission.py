"""创建的原锁覆盖首条intent、登记、取得占用和派发准备的原生写入。"""

from __future__ import annotations

import asyncio
from threading import get_ident

import pytest

from tests.unit.services.business.workspace_resources.creation.conftest import (
    CreationWrite,
    Operation,
    create,
)
from tests.unit.services.business.workspace_resources.test_record_write_lifetime import (
    OwnerFixture,
)


@pytest.mark.parametrize("creation_write", ["intent", "register", "acquire", "prepare"], indirect=True)
@pytest.mark.parametrize("interruption", ["cancel", "close"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_creation_interruption_waits_original_write_without_late_dispatch(
    resource_owner: OwnerFixture, operation: Operation, creation_write: CreationWrite,
    interruption: str, write_fails: bool,
) -> None:
    owner, gate = resource_owner.owner, creation_write
    assert owner is not None
    native = OSError(28, "原创建写入失败")
    gate.failure = native if write_fails else None
    creating = asyncio.create_task(create(operation))
    closing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        rid, = owner._resource_locks
        assert gate.writers == [gate.writers[0]] and gate.writers[0] != get_ident()
        lock = owner._resource_locks[rid]
        assert lock.locked() and resource_owner.transport.creates == 0
        if interruption == "cancel":
            creating.cancel("原创建取消")
            await asyncio.sleep(0)
            creating.cancel("重复取消不得遗弃创建")
        closing = asyncio.create_task(operation.close())
        async with asyncio.timeout(2):
            while not operation._closed:
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not closing.done() and not creating.done() and lock.locked()
        gate.release.set()
        if interruption == "cancel" and not (write_fails and gate.phase == "intent"):
            with pytest.raises(asyncio.CancelledError) as caught:
                await creating
            assert caught.value.args == ("原创建取消",)
            if write_fails:
                failure = caught.value.__cause__
                assert failure is not None and (failure is native or failure.__cause__ is native)
        elif write_fails:
            with pytest.raises(OSError) as caught_io:
                await creating
            assert caught_io.value is native or caught_io.value.__cause__ is native
            if interruption == "cancel":
                # 原记录writer交回迟到原生错误，原取消继续保存在context中。
                cancellation = caught_io.value.__context__
                assert isinstance(cancellation, asyncio.CancelledError)
                assert cancellation.args == ("原创建取消",)
        else:
            with pytest.raises(RuntimeError, match="operation 已关闭"):
                await creating
        await closing
        assert not lock.locked() and not operation._lease_refs and resource_owner.transport.creates == 0
        assert not operation._pending_resources
        record = owner._store.get(rid)
        if record is not None:
            assert record.status == "deleted" and record.reconciliation_status == "verified"
            assert record.owner_revision == 1 and not record.thread_associations
            external = resource_owner.leases.get(rid)
            assert external is not None and external.status == "stopped"
            assert not resource_owner.leases.leases_for_resource(rid)
        else:
            assert rid not in owner._resource_locks and resource_owner.leases.get(rid) is None
        cold = resource_owner.restart()
        assert cold._store.get(rid) == record
        if record is not None:
            assert not cold._leases.leases_for_resource(rid)
        else:
            assert cold._leases.get(rid) is None
        assert await cold.list_resources() == []
    finally:
        gate.release.set()
        await asyncio.gather(*([creating] if closing is None else [creating, closing]), return_exceptions=True)


@pytest.mark.parametrize("creation_write", ["prepare"], indirect=True)
async def test_prepare_write_failure_closes_original_intent_without_entering_external_callback(
    resource_owner: OwnerFixture, operation: Operation, creation_write: CreationWrite,
) -> None:
    owner, gate = resource_owner.owner, creation_write
    assert owner is not None
    gate.failure = OSError(28, "派发准备未持久")
    creating = asyncio.create_task(create(operation))
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        rid, = owner._store.ids()
        lease, = resource_owner.leases.leases_for_resource(rid)
        assert lease.status == "active"
        gate.release.set()
        with pytest.raises(OSError) as caught:
            await creating
        assert caught.value.__cause__ is gate.failure
        record = owner._store.get(rid)
        assert record is not None and record.status == "deleted" and record.reconciliation_status == "verified"
        settled = resource_owner.leases.get_lease(lease.lease_id)
        assert settled is not None and settled.status == "settled" and settled.operation_id == lease.operation_id
        await operation.close()
        assert resource_owner.leases.get_lease(lease.lease_id) == settled
        assert resource_owner.transport.creates == 0
    finally:
        gate.release.set()
        await asyncio.gather(creating, return_exceptions=True)
