"""孤儿回收中断不能丢失此前失败，也不能重放未知删除。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.services.business.workspace_resources import owner as owner_module
from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_workspace_resource_locking import (
    bundle as bundle,  # noqa: PLC0414
)


@pytest.mark.parametrize("ending", ["deadline", "cancel"])
async def test_interrupted_orphan_sweep_retains_ordered_failures_and_exact_original_leases(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch, ending: str,
) -> None:
    ids = [str((await bundle.create())["resource_id"]) for _ in range(4)]
    first, second, healthy, blocked = ids
    owner = bundle.owner
    assert owner is not None
    for rid in ids:
        await bundle.command(rid, action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
    bundle.now += timedelta(seconds=1800)
    deleting = bundle.transport.delete_resource
    failures = [TimeoutError("原删除回执自身超时"), ConnectionError("另一删除回执丢失")]
    first_failed, second_failed, peer_done, entered = (asyncio.Event() for _ in range(4))
    calls: list[str] = []
    deadlines: list[asyncio.Timeout] = []

    def capture(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        context = asyncio.timeout(seconds)
        current = asyncio.current_task()
        if current is not None and current.get_name() == "orphan-error-batch":
            deadlines.append(context)
        return context

    async def delete(resource_id: str) -> dict[str, object]:
        calls.append(resource_id)
        receipt = await deleting(resource_id)
        if resource_id == first:
            # 故意反序完成，错误仍须对应原资源选择顺序。
            await second_failed.wait()
            first_failed.set()
            raise failures[0]
        if resource_id == second:
            second_failed.set()
            raise failures[1]
        if resource_id == blocked:
            entered.set()
            await asyncio.Future()
        peer_done.set()
        return receipt

    with monkeypatch.context() as patch:
        patch.setattr(owner_module, "timeout", capture)
        patch.setattr(bundle.transport, "delete_resource", delete)
        sweeping = asyncio.create_task(owner.sweep_orphans(), name="orphan-error-batch")
        try:
            for barrier in (first_failed, peer_done, entered):
                await asyncio.wait_for(barrier.wait(), 2)
            # 外部回执返回不等于 owner 提交；等健康项原锁释放后才中断其它资源。
            async with asyncio.timeout(2), owner._resource_lock(healthy):
                assert not bundle.leases.leases_for_resource(healthy)
            leases = {rid: bundle.leases.leases_for_resource(rid)[0].lease_id for rid in (first, second, blocked)}
            ledger_ids = set(bundle.leases._leases)
            assert len(deadlines) == 1 and not sweeping.done()
            if ending == "deadline":
                deadlines[0].reschedule(asyncio.get_running_loop().time())
                with pytest.raises(ExceptionGroup) as caught:
                    await asyncio.wait_for(sweeping, 2)
                assert caught.value.exceptions[:2] == tuple(failures)
                expiry = caught.value.exceptions[2]
                assert isinstance(expiry, TimeoutError) and "孤儿回收等待超过 30 秒" in str(expiry)
                assert isinstance(expiry.__cause__, TimeoutError)
            else:
                sweeping.cancel("原孤儿回收调用者取消")
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await sweeping
                assert cancelled.value.args == ("原孤儿回收调用者取消",)
                cause = cancelled.value.__cause__
                assert isinstance(cause, ExceptionGroup) and cause.exceptions == tuple(failures)
            cold = ExternalResourceLeaseLedger(state_path=bundle.leases._state_path)
            assert set(cold._leases) == ledger_ids
            for rid, lease_id in leases.items():
                lease, = cold.leases_for_resource(rid)
                assert lease.lease_id == lease_id and lease.status == "reconcile_required"
                record = owner._store.get(rid)
                assert record is not None and record.owner_revision == 3
                assert record.status == "deleting" and record.reconciliation_status == "unknown"
            assert not cold.leases_for_resource(healthy)
            assert (await owner.get_resource(healthy))["status"] == "deleted"
            assert all(not owner._resource_lock(rid).locked() for rid in ids)
            assert await owner.sweep_orphans() == []
            assert calls == ids
        finally:
            if not sweeping.done():
                sweeping.cancel()
            await asyncio.gather(sweeping, return_exceptions=True)
    # 显式管理只核实已执行的终态，不重复发送删除，也不另建替代占用。
    for rid, lease_id in leases.items():
        result = await bundle.command(rid, action="delete", expected_revision=3)
        assert result["status"] == "deleted" and result["owner_revision"] == 3
        settled = bundle.leases.get_lease(lease_id)
        assert settled is not None and settled.status == "settled"
    assert calls == ids and bundle.transport.deleted == ids
    assert set(bundle.leases._leases) == ledger_ids
