"""授权准备写盘的原锁覆盖取消结算，未持久准备不得签发。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident

import pytest

from app.services.business.workspace_resources import operations as operations_module
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.workspace_resources.test_transport_grant_deadline import (
    bundle as bundle,  # noqa: PLC0414
)
from tests.unit.services.business.workspace_resources.test_transport_grant_deadline import (
    grant as grant,  # noqa: PLC0414
)


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_original_grant_preparation_write_settles_before_unlock_without_late_emission(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, grant,
    monkeypatch: pytest.MonkeyPatch, cancel: bool, write_fails: bool,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    before = owner._store.path.read_bytes()
    entered, free, loop = asyncio.Event(), Event(), asyncio.get_running_loop()
    replace, issue = Path.replace, bundle.transport.issue_transport_grant
    writes = 0
    issued: list[str] = []
    native = OSError(28, "授权派发准备写入失败")
    writers: list[int] = []

    def publishing(source: Path, target: Path) -> Path:
        nonlocal writes
        if target == path:
            writes += 1
            if writes == 2:
                writers.append(get_ident())
                loop.call_soon_threadsafe(entered.set)
                assert free.wait(5), "测试未释放授权准备的原写入"
                if write_fails:
                    raise native
        return replace(source, target)

    async def issuing(resource_id: str, **arguments):
        issued.append(resource_id)
        return await issue(resource_id, **arguments)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "replace", publishing)
        fault.setattr(bundle.transport, "issue_transport_grant", issuing)
        pending = asyncio.create_task(grant(rid))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert writers and writers[0] != get_ident() and owner._resource_lock(rid).locked()
            lease, = bundle.leases.leases_for_resource(rid)
            assert lease.status == "active" and not pending.done() and issued == []
            if cancel:
                pending.cancel("原授权准备取消")
                await asyncio.sleep(0)
                pending.cancel("重复取消保留原授权准备")
                await asyncio.sleep(0)
                assert not pending.done() and owner._resource_lock(rid).locked()
            free.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError) as caught:
                    await pending
                assert caught.value.args == ("原授权准备取消",)
                if write_fails:
                    assert caught.value.__cause__ is not None and caught.value.__cause__.__cause__ is native
            elif write_fails:
                with pytest.raises(OSError) as caught_io:
                    await pending
                assert caught_io.value.__cause__ is native
            else:
                assert (await pending)["resource"]["resource_id"] == rid
            assert issued == ([] if cancel or write_fails else [rid])
            if cancel or write_fails:
                assert owner._store.path.read_bytes() == before
            released = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
            assert released is not None and released.status == "released" and released.operation_id == lease.operation_id
            assert not owner._resource_lock(rid).locked()
        finally:
            free.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_queued_cancelled_grant_does_not_write_preparation_or_emit_token(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, grant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    owner = bundle.owner
    assert owner is not None
    loop, occupied, queued, free = asyncio.get_running_loop(), asyncio.Event(), asyncio.Event(), Event()
    occupying: asyncio.Future[None] | None = None
    run = operations_module.run_sync_shielded
    mark = bundle.leases.mark_operation_dispatched
    marks: list[str] = []
    issued: list[str] = []
    issue = bundle.transport.issue_transport_grant

    def occupy() -> None:
        loop.call_soon_threadsafe(occupied.set)
        assert free.wait(5), "测试未释放原授权线程池占用"

    async def intercept(function, *args, **kwargs):
        nonlocal occupying
        if function.__name__ == "prepare":
            occupying = loop.run_in_executor(None, occupy)
            await occupied.wait()
            loop.call_soon(queued.set)
        return await run(function, *args, **kwargs)

    def marked(lease_id: str) -> None:
        marks.append(lease_id)
        mark(lease_id)

    async def issuing(resource_id: str, **arguments):
        issued.append(resource_id)
        return await issue(resource_id, **arguments)

    with ThreadPoolExecutor(max_workers=1) as executor, monkeypatch.context() as fault:
        fault.setattr(loop, "_default_executor", executor)
        fault.setattr(operations_module, "run_sync_shielded", intercept)
        fault.setattr(bundle.leases, "mark_operation_dispatched", marked)
        fault.setattr(bundle.transport, "issue_transport_grant", issuing)
        pending = asyncio.create_task(grant(rid))
        try:
            await asyncio.wait_for(queued.wait(), 2)
            lease, = bundle.leases.leases_for_resource(rid)
            pending.cancel("排队授权原取消")
            await asyncio.sleep(0)
            pending.cancel("重复取消不得遗弃排队")
            await asyncio.sleep(0)
            assert not pending.done() and owner._resource_lock(rid).locked()
            assert marks == issued == []
            free.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await pending
            assert caught.value.args == ("排队授权原取消",)
            assert marks == issued == []
            released = bundle.leases.get_lease(lease.lease_id)
            assert released is not None and released.status == "released"
            assert not owner._resource_lock(rid).locked()
        finally:
            free.set()
            await asyncio.gather(pending, return_exceptions=True)
            if occupying is not None:
                await occupying
