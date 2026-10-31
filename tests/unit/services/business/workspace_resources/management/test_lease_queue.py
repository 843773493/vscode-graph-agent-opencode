"""原管理lease在线程池排队时取消或到期，不迟到取得或派发。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import TypeVar

import pytest

from app.services.business.workspace_resources import owner as owner_module
from tests.unit.services.business.workspace_resources.management.test_lease_io import (
    ManagementCase,
)
from tests.unit.services.business.workspace_resources.management.test_lease_io import (
    management_case as management_case,  # noqa: PLC0414
)

T = TypeVar("T")


@pytest.mark.parametrize("management_case", ["terminal-command", "terminal-grant", "browser-command", "browser-grant"], indirect=True)
@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
async def test_queued_management_admission_keeps_holder_and_never_late_acquires(
    management_case: ManagementCase, monkeypatch: pytest.MonkeyPatch, interruption: str,
) -> None:
    case = management_case
    owner, path = case.bundle.owner, case.bundle.leases._state_path
    assert owner is not None and path is not None
    loop, occupied, queued, free = asyncio.get_running_loop(), asyncio.Event(), asyncio.Event(), Event()
    occupying: asyncio.Future[None] | None = None
    run, timeout = owner_module.run_sync_shielded, owner_module.timeout
    budgets: list[asyncio.Timeout] = []
    before, ledger = owner._store.path.read_bytes(), path.read_bytes()

    def occupy() -> None:
        loop.call_soon_threadsafe(occupied.set)
        assert free.wait(5), "测试未释放原管理lease线程池占用"

    async def intercept(function: Callable[[], T]) -> T:
        nonlocal occupying
        if function.__name__ == "acquire":
            occupying = loop.run_in_executor(None, occupy)
            await occupied.wait()
            loop.call_soon(queued.set)
        return await run(function)

    def capture(seconds: float) -> asyncio.Timeout:
        context = timeout(seconds)
        task = asyncio.current_task()
        if task is not None and task.get_name() == "management-lease-deadline":
            budgets.append(context)
        return context

    with ThreadPoolExecutor(max_workers=1) as executor, monkeypatch.context() as patch:
        patch.setattr(loop, "_default_executor", executor)
        patch.setattr(owner_module, "run_sync_shielded", intercept)
        patch.setattr(owner_module, "timeout", capture)
        pending = asyncio.create_task(case.run(), name="management-lease-deadline")
        deletion: asyncio.Task[None] | None = None

        async def deleting() -> None:
            async with owner.deletion_holder.lock_for(case.thread.session_id, exclusive=True):
                pass

        try:
            await asyncio.wait_for(queued.wait(), 2)
            assert owner._resource_lock(case.resource_id).locked() and not pending.done()
            assert len(budgets) == 1
            if interruption == "cancel":
                pending.cancel("原排队管理请求取消")
            else:
                budgets[0].reschedule(loop.time() - 1)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            deletion = asyncio.create_task(deleting())
            await asyncio.sleep(0)
            assert not pending.done() and not deletion.done()
            assert path.read_bytes() == ledger and owner._store.path.read_bytes() == before and case.calls == []
            free.set()
            if interruption == "cancel":
                with pytest.raises(asyncio.CancelledError) as caught:
                    await asyncio.wait_for(pending, 2)
                assert caught.value.args == ("原排队管理请求取消",)
            else:
                with pytest.raises(TimeoutError, match="管理命令等待超过 30 秒|传输授权等待超过 30 秒"):
                    await asyncio.wait_for(pending, 2)
                assert budgets[0].expired()
            await asyncio.wait_for(deletion, 2)
            assert path.read_bytes() == ledger and owner._store.path.read_bytes() == before and case.calls == []
            assert not owner._resource_lock(case.resource_id).locked()
            assert case.bundle.leases.leases_for_resource(case.resource_id) == []
        finally:
            free.set()
            await asyncio.gather(pending, *([] if occupying is None else [occupying]),
                                 *([] if deletion is None else [deletion]), return_exceptions=True)
