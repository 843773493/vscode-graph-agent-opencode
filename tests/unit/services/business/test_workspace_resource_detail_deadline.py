"""资源详情共用恢复、锁和清单等待预算，不把读取到期当成释放证明。"""

from __future__ import annotations

import asyncio

import pytest

from app.services.business.workspace_resources import owner as owner_module
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_workspace_resource_locking import (
    bundle as bundle,  # noqa: PLC0414
)


@pytest.fixture
def deadlines(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Timeout]:
    captured: list[asyncio.Timeout] = []

    def capture(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        context = asyncio.timeout(seconds)
        if asyncio.current_task().get_name() == "detail-under-test":
            captured.append(context)
        return context

    monkeypatch.setattr(owner_module, "timeout", capture)
    return captured


@pytest.mark.parametrize("stage", ["recovery", "lock", "receipt"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_detail_deadline_or_caller_cancel_drains_only_its_waiter(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    deadlines: list[asyncio.Timeout], stage: str, cancel: bool,
) -> None:
    first, peer = await bundle.create(), await bundle.create()
    owner = bundle.owner
    assert owner is not None
    rid = str(first["resource_id"])
    entered, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    recover, listing = owner.recover, bundle.transport.list_resources
    lock = owner._resource_lock(rid)
    calls: list[str] = []

    async def recovering(resource_ids=None) -> None:
        if asyncio.current_task().get_name() == "detail-under-test":
            if stage == "recovery":
                entered.set()
                try:
                    await release.wait()
                finally:
                    drained.set()
                return
            await recover(resource_ids)
            if stage == "lock":
                await lock.acquire()
                entered.set()
            return
        await recover(resource_ids)

    async def receiving():
        result = await listing()
        if asyncio.current_task().get_name() == "detail-under-test":
            calls.append(rid)
            if stage == "receipt":
                entered.set()
                try:
                    await release.wait()
                finally:
                    drained.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(owner, "recover", recovering)
        patch.setattr(bundle.transport, "list_resources", receiving)
        pending = asyncio.create_task(owner.get_resource(rid), name="detail-under-test")
        try:
            await asyncio.wait_for(entered.wait(), 1)
            independent = await asyncio.wait_for(owner.get_resource(str(peer["resource_id"])), 1)
            assert independent["status"] == "running" and independent["owner_revision"] == 1
            before, ledger = owner._store.path.read_bytes(), bundle.leases._state_path.read_bytes()
            if cancel:
                pending.cancel("原详情调用取消")
                with pytest.raises(asyncio.CancelledError, match="原详情调用取消"):
                    await pending
            else:
                if deadlines:
                    deadlines[0].reschedule(deadlines[0].when() - 31)
                with pytest.raises(TimeoutError, match="详情等待超过 30 秒"):
                    await asyncio.wait_for(asyncio.shield(pending), 0.3)
                assert len(deadlines) == 1 and deadlines[0].expired()
            assert owner._store.path.read_bytes() == before
            assert bundle.leases._state_path.read_bytes() == ledger
            assert calls == ([rid] if stage == "receipt" else [])
            if stage == "lock":
                assert lock.locked()
            else:
                assert drained.is_set()
            release.set()
            if stage == "lock":
                lock.release()
            await asyncio.sleep(0)
            assert owner._store.path.read_bytes() == before
            assert bundle.leases._state_path.read_bytes() == ledger
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            release.set()
            if stage == "lock" and lock.locked():
                lock.release()
    retried = await owner.get_resource(rid)
    assert retried["resource_id"] == rid and retried["owner_revision"] == 1
    assert retried["status"] == "running" and retried["reconciliation_status"] == "verified"
    assert not bundle.leases.leases_for_resource(rid)


async def test_detail_retains_committed_cold_recovery_when_the_shared_budget_expires(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    deadlines: list[asyncio.Timeout],
) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    owner = bundle.restart()
    sync, listing = bundle.transport.sync_owner_revision, bundle.transport.list_resources
    entered, release = asyncio.Event(), asyncio.Event()
    revisions: list[int] = []

    async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
        await sync(resource_id, revision, for_stop=for_stop)
        revisions.append(revision)
        if deadlines:
            deadlines[0].reschedule(deadlines[0].when() - 20)

    async def receiving():
        result = await listing()
        if revisions and asyncio.current_task().get_name() == "detail-under-test":
            entered.set()
            await release.wait()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "sync_owner_revision", syncing)
        patch.setattr(bundle.transport, "list_resources", receiving)
        pending = asyncio.create_task(owner.get_resource(rid), name="detail-under-test")
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert revisions == [2]
            assert owner._store.get(rid).owner_revision == 2
            assert owner._store.get(rid).reconciliation_status == "verified"
            before, ledger = owner._store.path.read_bytes(), bundle.leases._state_path.read_bytes()
            if deadlines:
                deadlines[0].reschedule(deadlines[0].when() - 11)
            with pytest.raises(TimeoutError, match="详情等待超过 30 秒"):
                await asyncio.wait_for(asyncio.shield(pending), 0.3)
            assert len(deadlines) == 1 and deadlines[0].expired()
            assert owner._store.path.read_bytes() == before
            assert bundle.leases._state_path.read_bytes() == ledger
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            release.set()
    retried = await owner.get_resource(rid)
    assert retried["owner_revision"] == 2 and retried["reconciliation_status"] == "verified"
    assert revisions == [2]


async def test_detail_preserves_original_transport_timeout(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = await bundle.create()
    owner = bundle.owner
    assert owner is not None
    original = TimeoutError("原传输超时")
    before, ledger = owner._store.path.read_bytes(), bundle.leases._state_path.read_bytes()

    async def failed():
        raise original

    monkeypatch.setattr(bundle.transport, "list_resources", failed)
    with pytest.raises(TimeoutError) as caught:
        await owner.get_resource(str(resource["resource_id"]))
    assert caught.value is original
    assert owner._store.path.read_bytes() == before
    assert bundle.leases._state_path.read_bytes() == ledger
