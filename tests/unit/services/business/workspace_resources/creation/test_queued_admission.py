"""线程池排队时取消或关闭，不迟到登记占用或调用外部创建。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.services.business.workspace_resources import operations as operations_module
from tests.unit.services.business.workspace_resources.creation.conftest import (
    Operation,
    create,
)
from tests.unit.services.business.workspace_resources.test_record_write_lifetime import (
    OwnerFixture,
)


@pytest.mark.parametrize("phase", ["acquire", "prepare"])
@pytest.mark.parametrize("interruption", ["cancel", "close"])
async def test_queued_creation_does_not_start_new_lease_write_after_interruption(
    resource_owner: OwnerFixture, operation: Operation,
    monkeypatch: pytest.MonkeyPatch, phase: str, interruption: str,
) -> None:
    owner, ledger_path = resource_owner.owner, resource_owner.leases._state_path
    assert owner is not None and ledger_path is not None
    loop, queued, free = asyncio.get_running_loop(), asyncio.Event(), Event()
    occupying: asyncio.Future[None] | None = None
    run = operations_module.run_sync_shielded
    originals = {name: getattr(resource_owner.leases, name)
                 for name in ("register_external", "acquire_operation", "mark_operation_dispatched")}
    calls: list[str] = []

    def occupy() -> None:
        loop.call_soon_threadsafe(queued.set)
        assert free.wait(5), "测试未释放原线程池占用"

    async def intercept(function, *args, **kwargs):
        nonlocal occupying
        if function.__name__ == phase:
            occupying = loop.run_in_executor(None, occupy)
            await queued.wait()
        return await run(function, *args, **kwargs)

    def track(name: str):
        def invoke(*args, **kwargs):
            calls.append(name)
            return originals[name](*args, **kwargs)
        return invoke

    with ThreadPoolExecutor(max_workers=1) as executor, monkeypatch.context() as patch:
        patch.setattr(loop, "_default_executor", executor)
        patch.setattr(operations_module, "run_sync_shielded", intercept)
        for name in originals:
            patch.setattr(resource_owner.leases, name, track(name))
        creating = asyncio.create_task(create(operation))
        closing: asyncio.Task[None] | None = None
        try:
            await asyncio.wait_for(queued.wait(), 2)
            rid, = owner._store.ids()
            lock = owner._resource_locks[rid]
            before = list(calls)
            assert before == ([] if phase == "acquire" else ["register_external", "acquire_operation"])
            if interruption == "cancel":
                creating.cancel("排队创建原取消")
                await asyncio.sleep(0)
                creating.cancel("重复取消保留原排队")
            closing = asyncio.create_task(operation.close())
            async with asyncio.timeout(2):
                while not operation._closed:
                    await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not creating.done() and not closing.done() and lock.locked()
            assert calls == before and resource_owner.transport.creates == 0
            free.set()
            with pytest.raises(asyncio.CancelledError if interruption == "cancel" else RuntimeError) as caught:
                await creating
            if interruption == "cancel":
                assert caught.value.args == ("排队创建原取消",)
            else:
                assert "operation 已关闭" in str(caught.value)
            await closing
            # 仅未派发intent的正式收尾可登记资源；不能迟到acquire或mark。
            assert calls == [*before, "register_external"]
            record = owner._store.get(rid)
            assert record is not None and record.status == "deleted" and record.reconciliation_status == "verified"
            assert not owner._leases.leases_for_resource(rid) and not lock.locked()
            assert not operation._lease_refs and not operation._pending_resources
            assert resource_owner.transport.creates == 0
        finally:
            free.set()
            tasks = [creating] if closing is None else [creating, closing]
            await asyncio.gather(*tasks, return_exceptions=True)
            if occupying is not None:
                await occupying
