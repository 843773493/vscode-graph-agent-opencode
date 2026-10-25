"""整组关联释放保留全量冲突检查，各资源独立收敛并共用入口截止。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from app.services.business.workspace_resources import owner as owner_module
from app.services.business.workspace_resources.owner import ResourceConflict
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_workspace_resource_locking import (
    bundle as bundle,  # noqa: PLC0414
)


@pytest.fixture(params=["thread", "session"])
def releasing(request: pytest.FixtureRequest, bundle: TerminalOwnerFixture | BrowserOwnerFixture) -> Callable[[], Awaitable[int | None]]:
    async def run() -> int | None:
        if request.param == "thread":
            return await bundle.owner.release_thread(SESSION_A, THREAD_A, 1)
        return await bundle.owner.cleanup_session(SESSION_A)
    return run


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


@pytest.mark.parametrize("scope", ["thread", "workspace"])
async def test_release_completes_and_unlocks_peer_while_first_revision_waits(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, releasing: Callable[[], Awaitable[int | None]],
    monkeypatch: pytest.MonkeyPatch, scope: str,
) -> None:
    first, second = await bundle.create(scope=scope), await bundle.create(scope=scope)
    rid, peer = str(first["resource_id"]), str(second["resource_id"])
    entered, release, peer_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = bundle.transport.sync_owner_revision

    async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
        if resource_id == rid:
            entered.set()
            await release.wait()
        await original(resource_id, revision, for_stop=for_stop)
        if resource_id == peer:
            peer_done.set()

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", syncing)
    task = asyncio.create_task(releasing())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(peer_done.wait(), 2)
        current = await asyncio.wait_for(bundle.owner.get_resource(peer), 2)
        assert current["owner_revision"] == 2 and current["thread_associations"] == []
        assert current["reconciliation_status"] == "verified"
        assert current["status"] == ("deleted" if scope == "thread" else second["status"])
        assert not bundle.leases.leases_for_resource(peer) and not task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.exception() is None
    assert set(bundle.transport.deleted) == ({rid, peer} if scope == "thread" else set())


async def test_release_conflict_checks_entire_set_without_partial_detach(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, releasing: Callable[[], Awaitable[int | None]],
) -> None:
    first, second = await bundle.create(scope="workspace"), await bundle.create(scope="workspace")
    rid, peer = str(first["resource_id"]), str(second["resource_id"])
    async with bundle.owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A, holder_id="product-operation", operation_id="hold-peer",
    ) as operation:
        if isinstance(bundle, TerminalOwnerFixture):
            await operation.get_resource(peer)
        else:
            await operation.perform(peer, "navigate", {"url": "https://lease.test"})
        before = [bundle.owner._store.get(item) for item in (rid, peer)]
        ledger = bundle.leases._state_path.read_bytes()
        with pytest.raises(ResourceConflict, match="未结清"):
            await releasing()
        assert [bundle.owner._store.get(item) for item in (rid, peer)] == before
        assert bundle.leases._state_path.read_bytes() == ledger
        assert not any(bundle.owner._resource_lock(item).locked() for item in (rid, peer))


async def test_release_reports_original_errors_in_order_after_peer_finishes(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, releasing: Callable[[], Awaitable[int | None]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [str((await bundle.create())["resource_id"]) for _ in range(3)]
    errors = [ConnectionError("首项删除回执未知"), TimeoutError("传输自身超时")]
    original = bundle.transport.delete_resource

    async def deleting(resource_id: str) -> dict[str, object]:
        if resource_id in ids[:2]:
            raise errors[ids.index(resource_id)]
        return await original(resource_id)

    monkeypatch.setattr(bundle.transport, "delete_resource", deleting)
    with pytest.raises(ExceptionGroup) as caught:
        await releasing()
    assert list(caught.value.exceptions) == errors
    assert bundle.transport.deleted == [ids[2]]
    assert (await bundle.owner.get_resource(ids[2]))["reconciliation_status"] == "verified"
    for rid in ids[:2]:
        assert bundle.owner._store.get(rid).reconciliation_status == "unknown"
        lease, = bundle.leases.leases_for_resource(rid)
        assert lease.status == "reconcile_required"
    assert not any(bundle.owner._resource_lock(item).locked() for item in ids)


@pytest.mark.parametrize("phase", ["prepare", "recovery", "preflight_lock", "observation", "revision", "delete"])
async def test_release_deadline_covers_preflight_and_dispatch_without_false_release(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, releasing: Callable[[], Awaitable[int | None]], deadlines: list[asyncio.Timeout],
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    ids = [str((await bundle.create())["resource_id"]) for _ in range(2)]
    rid, peer = ids
    entered, release, peer_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_delete = bundle.transport.delete_resource
    if phase == "observation":
        async def missing_receipt(resource_id: str) -> dict[str, object]:
            if resource_id == rid:
                raise ConnectionError("尚无停止证明")
            return await original_delete(resource_id)
        with monkeypatch.context() as patch:
            patch.setattr(bundle.transport, "delete_resource", missing_receipt)
            with pytest.raises(ConnectionError):
                await releasing()
    deadlines.clear()
    if phase == "recovery":
        bundle.restart()
    owner = bundle.owner
    original_recover, original_get, original_sync = owner.recover, bundle.transport.get_resource, bundle.transport.sync_owner_revision

    async def recovering(resource_ids=None) -> None:
        if phase == "prepare" and resource_ids == ():
            entered.set()
            await release.wait()
        await original_recover(resource_ids)

    async def getting(resource_id: str) -> dict[str, object]:
        if phase == "observation" and resource_id == rid:
            entered.set()
            await release.wait()
        return await original_get(resource_id)

    async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
        if resource_id == rid and ((phase == "revision" and for_stop) or (phase == "recovery" and not for_stop)):
            entered.set()
            await release.wait()
        await original_sync(resource_id, revision, for_stop=for_stop)

    async def deleting(resource_id: str) -> dict[str, object]:
        if resource_id == rid and phase == "delete":
            entered.set()
            await release.wait()
        result = await original_delete(resource_id)
        if resource_id == peer:
            peer_done.set()
        return result

    monkeypatch.setattr(owner, "recover", recovering)
    monkeypatch.setattr(bundle.transport, "get_resource", getting)
    monkeypatch.setattr(bundle.transport, "sync_owner_revision", syncing)
    monkeypatch.setattr(bundle.transport, "delete_resource", deleting)
    held = owner._resource_lock(max(ids)) if phase == "preflight_lock" else None
    if held is not None:
        await held.acquire()
    before = [owner._store.get(item) for item in ids]
    task = asyncio.create_task(releasing())
    try:
        if held is None:
            await asyncio.wait_for(entered.wait(), 2)
        else:
            # 等待释放入口确实走到已持有的集合锁，不用缩短生产期限。
            async with asyncio.timeout(2):
                while not held._waiters:
                    await asyncio.sleep(0)
        if phase in {"revision", "delete"}:
            await asyncio.wait_for(peer_done.wait(), 2)
        assert len(deadlines) == 1
        pending = bundle.leases.leases_for_resource(rid)
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError, match="关联释放等待超过 30 秒"):
            await asyncio.wait_for(task, 2)
        if phase in {"revision", "delete"}:
            lease, = bundle.leases.leases_for_resource(rid)
            assert lease.lease_id == pending[0].lease_id and lease.status == "reconcile_required"
            assert owner._store.get(rid).reconciliation_status == "unknown"
            assert not bundle.leases.leases_for_resource(peer)
            assert (await owner.get_resource(peer))["status"] == "deleted"
        elif phase in {"prepare", "preflight_lock", "observation"}:
            assert [owner._store.get(item) for item in ids] == before
        else:
            assert owner._store.get(rid).reconciliation_status == "unknown"
            assert all(owner._store.get(item).thread_associations == before[n].thread_associations for n, item in enumerate(ids))
        assert not any(owner._resource_lock(item).locked() for item in ids if held is not owner._resource_lock(item))
    finally:
        release.set()
        if held is not None:
            held.release()
        await asyncio.gather(task, return_exceptions=True)


async def test_external_cancellation_keeps_original_lease_and_single_transport_timeout_is_not_relabelled(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, releasing: Callable[[], Awaitable[int | None]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def deleting(resource_id: str) -> dict[str, object]:
        calls.append(resource_id)
        entered.set()
        await release.wait()
        raise AssertionError("取消后不得继续删除")

    monkeypatch.setattr(bundle.transport, "delete_resource", deleting)
    task = asyncio.create_task(releasing())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pending, = bundle.leases.leases_for_resource(rid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lease, = bundle.leases.leases_for_resource(rid)
        assert lease.lease_id == pending.lease_id and lease.status == "reconcile_required"
        assert not bundle.owner._resource_lock(rid).locked()
        with pytest.raises(ResourceConflict):
            await releasing()
        assert calls == [rid]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)

    # 原未知资源留待管理核实；新的资源用于验证传输自身的错误归属。
    error = TimeoutError("原传输截止")
    peer = str((await bundle.create(thread=bundle.sessions.thread_ids_by_session[SESSION_A][1]))["resource_id"])

    async def timed_out(resource_id: str) -> dict[str, object]:
        raise error

    monkeypatch.setattr(bundle.transport, "delete_resource", timed_out)
    with pytest.raises(TimeoutError) as caught:
        await bundle.owner.release_thread(SESSION_A, bundle.sessions.thread_ids_by_session[SESSION_A][1], 1)
    assert caught.value is error
    assert bundle.owner._store.get(peer).reconciliation_status == "unknown"


async def test_unknown_observation_does_not_lock_peer_or_detach_before_preflight(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, releasing: Callable[[], Awaitable[int | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid, peer = [str((await bundle.create())["resource_id"]) for _ in range(2)]

    async def missing_receipt(resource_id: str, *, reason: str | None = None) -> dict[str, object]:
        raise ConnectionError("停止未取得回执")

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "stop_resource", missing_receipt)
        with pytest.raises(ConnectionError):
            await bundle.command(rid, action="stop", expected_revision=1)
    before = [bundle.owner._store.get(item) for item in (rid, peer)]
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.transport.get_resource

    async def getting(resource_id: str) -> dict[str, object]:
        if resource_id == rid:
            entered.set()
            await release.wait()
        return await original(resource_id)

    monkeypatch.setattr(bundle.transport, "get_resource", getting)
    task = asyncio.create_task(releasing())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        current = await asyncio.wait_for(bundle.owner.get_resource(peer), 2)
        assert current["thread_associations"] == [{"session_id": SESSION_A, "thread_id": THREAD_A}]
        assert [bundle.owner._store.get(item) for item in (rid, peer)] == before
        assert not task.done()
    finally:
        release.set()
        result, = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result, ResourceConflict)
    assert [bundle.owner._store.get(item) for item in (rid, peer)] == before
    assert not bundle.transport.deleted
