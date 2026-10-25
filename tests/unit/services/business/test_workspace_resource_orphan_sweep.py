"""孤儿回收独立推进、整批截止与原占用保留。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.services.business.workspace_resources import owner as owner_module
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_workspace_resource_locking import (
    bundle as bundle,  # noqa: PLC0414
)


@pytest.fixture
async def due_resources(bundle: TerminalOwnerFixture | BrowserOwnerFixture) -> list[str]:
    from app.services.business.workspace_resources.records import ThreadRef

    ids = [str((await bundle.create())["resource_id"]) for _ in range(2)]
    for rid in ids:
        await bundle.command(rid, action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
    bundle.now += timedelta(seconds=1800)
    return ids


@pytest.fixture
def deadlines(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Timeout]:
    contexts: list[asyncio.Timeout] = []

    def capture(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        context = asyncio.timeout(seconds)
        contexts.append(context)
        return context

    monkeypatch.setattr(owner_module, "timeout", capture, raising=False)
    return contexts


@pytest.mark.parametrize("phase", ["recovery", "resource_lock", "delete"])
async def test_blocked_orphan_does_not_delay_the_other_original_reclaim(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, due_resources: list[str],
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    rid, peer = due_resources
    owner = bundle.restart() if phase == "recovery" else bundle.owner
    entered, release, peer_deleted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_delete, original_sync = bundle.transport.delete_resource, bundle.transport.sync_owner_revision

    async def deleting(resource_id: str) -> dict[str, object]:
        if resource_id == rid and phase == "delete":
            entered.set()
            await release.wait()
        result = await original_delete(resource_id)
        if resource_id == peer:
            peer_deleted.set()
        return result

    async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
        if resource_id == rid and phase == "recovery" and not for_stop:
            entered.set()
            await release.wait()
        await original_sync(resource_id, revision, for_stop=for_stop)

    monkeypatch.setattr(bundle.transport, "delete_resource", deleting)
    monkeypatch.setattr(bundle.transport, "sync_owner_revision", syncing)
    lock = owner._resource_lock(rid)
    if phase == "resource_lock":
        await lock.acquire()
        entered.set()
    sweeping = asyncio.create_task(owner.sweep_orphans())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(peer_deleted.wait(), 2)
        current = await asyncio.wait_for(owner.get_resource(peer), 2)
        assert current["status"] == "deleted" and current["reconciliation_status"] == "verified"
        assert not bundle.leases.leases_for_resource(peer) and not sweeping.done()
    finally:
        release.set()
        if phase == "resource_lock":
            lock.release()
        results = await asyncio.gather(sweeping, return_exceptions=True)
    assert isinstance(results[0], list)
    assert [item["resource_id"] for item in results[0]] == due_resources
    assert set(bundle.transport.deleted) == set(due_resources)


@pytest.mark.parametrize("phase", ["prepare", "recovery", "resource_lock", "revision", "delete"])
async def test_sweep_deadline_covers_all_phases_without_false_release_or_automatic_replay(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, due_resources: list[str],
    deadlines: list[asyncio.Timeout], monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    rid, peer = due_resources
    owner = bundle.restart() if phase in {"prepare", "recovery"} else bundle.owner
    entered, release, peer_deleted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_delete = bundle.transport.delete_resource
    original_sync = bundle.transport.sync_owner_revision
    original_list = bundle.transport.list_resources
    deletes: list[str] = []

    async def listing() -> list[dict[str, object]]:
        if phase == "prepare":
            entered.set()
            await release.wait()
        return await original_list()

    async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
        await original_sync(resource_id, revision, for_stop=for_stop)
        if resource_id == rid and ((phase == "revision" and for_stop) or (phase == "recovery" and not for_stop)):
            entered.set()
            await release.wait()

    async def deleting(resource_id: str) -> dict[str, object]:
        deletes.append(resource_id)
        result = await original_delete(resource_id)
        if resource_id == rid and phase == "delete":
            entered.set()
            await release.wait()
        if resource_id == peer:
            peer_deleted.set()
        return result

    monkeypatch.setattr(bundle.transport, "list_resources", listing)
    monkeypatch.setattr(bundle.transport, "sync_owner_revision", syncing)
    monkeypatch.setattr(bundle.transport, "delete_resource", deleting)
    lock = owner._resource_lock(rid)
    before, ledger = owner._store.get(rid), bundle.leases._state_path.read_bytes()
    if phase == "resource_lock":
        await lock.acquire()
        entered.set()
    sweeping = asyncio.create_task(owner.sweep_orphans())
    original_lease = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if phase != "prepare":
            await asyncio.wait_for(peer_deleted.wait(), 2)
        assert len(deadlines) == 1 and deadlines[0].when() is not None
        if phase in {"revision", "delete"}:
            original_lease, = bundle.leases.leases_for_resource(rid)
            assert original_lease.status == "active"
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError, match="孤儿回收等待超过 30 秒"):
            await asyncio.wait_for(sweeping, 2)
        if original_lease is None:
            if phase == "recovery":
                assert owner._recovery._pending == {rid: before.owner_revision + 1}
                assert owner._store.get(rid).owner_revision == before.owner_revision + 1
                assert owner._store.get(rid).reconciliation_status == "unknown"
            else:
                assert owner._store.get(rid) == before
            assert not bundle.leases.leases_for_resource(rid)
            if phase == "prepare":
                assert bundle.leases._state_path.read_bytes() == ledger
                assert not deletes
        else:
            pending, = bundle.leases.leases_for_resource(rid)
            assert pending.lease_id == original_lease.lease_id and pending.status == "reconcile_required"
            assert owner._store.get(rid).reconciliation_status == "unknown"
            assert owner._store.get(rid).owner_revision == before.owner_revision + 1
            assert not lock.locked()
            count = deletes.count(rid)
            assert await owner.sweep_orphans() == []
            assert deletes.count(rid) == count
    finally:
        release.set()
        if phase == "resource_lock":
            lock.release()
        await asyncio.gather(sweeping, return_exceptions=True)
    if original_lease is not None or phase == "recovery":
        record = owner._store.get(rid)
        result = await bundle.command(rid, action="delete", expected_revision=record.owner_revision)
        assert result["status"] == "deleted" and result["reconciliation_status"] == "verified"
        if original_lease is not None:
            assert bundle.leases.get_lease(original_lease.lease_id).status == "settled"
        # 已执行删除但丢回执的管理重试只对账，不再发送删除。
        assert deletes.count(rid) == 1


async def test_transport_timeout_keeps_original_error_and_other_reclaims_complete(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, due_resources: list[str],
    deadlines: list[asyncio.Timeout], monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid, peer = due_resources
    original = bundle.transport.delete_resource
    error = TimeoutError("传输自身截止")

    async def deleting(resource_id: str) -> dict[str, object]:
        if resource_id == rid:
            raise error
        return await original(resource_id)

    monkeypatch.setattr(bundle.transport, "delete_resource", deleting)
    with pytest.raises(ExceptionGroup) as caught:
        await bundle.owner.sweep_orphans()
    assert caught.value.exceptions == (error,)
    assert len(deadlines) == 1 and not deadlines[0].expired()
    assert bundle.owner._store.get(peer).status == "deleted"
    assert bundle.owner._store.get(rid).reconciliation_status == "unknown"
    pending, = bundle.leases.leases_for_resource(rid)
    assert pending.status == "reconcile_required"


async def test_external_cancellation_keeps_original_pending_lease_and_drains_waiters(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, due_resources: list[str],
    deadlines: list[asyncio.Timeout], monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid, peer = due_resources
    entered, peer_deleted = asyncio.Event(), asyncio.Event()
    original = bundle.transport.delete_resource

    async def deleting(resource_id: str) -> dict[str, object]:
        result = await original(resource_id)
        if resource_id == rid:
            entered.set()
            await asyncio.Event().wait()
        else:
            peer_deleted.set()
        return result

    monkeypatch.setattr(bundle.transport, "delete_resource", deleting)
    sweeping = asyncio.create_task(bundle.owner.sweep_orphans())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(peer_deleted.wait(), 2)
        pending, = bundle.leases.leases_for_resource(rid)
        sweeping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweeping
        assert not deadlines[0].expired()
        assert not bundle.owner._resource_lock(rid).locked()
        assert bundle.owner._store.get(peer).status == "deleted"
        saved, = bundle.leases.leases_for_resource(rid)
        assert saved.lease_id == pending.lease_id and saved.status == "reconcile_required"
        assert bundle.owner._store.get(rid).reconciliation_status == "unknown"
    finally:
        sweeping.cancel()
        await asyncio.gather(sweeping, return_exceptions=True)
