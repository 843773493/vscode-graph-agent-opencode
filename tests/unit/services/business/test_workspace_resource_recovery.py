"""冷恢复总等待预算；不把请求取消或锁超时当作外部终态。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.business.workspace_resources import recovery
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)


@pytest.fixture(params=["terminal", "browser"])
def bundle(request: pytest.FixtureRequest, tmp_path: Path) -> TerminalOwnerFixture | BrowserOwnerFixture:
    factory = build_terminal_owner_fixture if request.param == "terminal" else build_browser_owner_fixture
    return factory(tmp_path)


@pytest.fixture
def deadlines(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Timeout]:
    """保留真实30秒合同，只在屏障到达后推进该调用的截止事件。"""
    contexts: list[asyncio.Timeout] = []
    original = asyncio.timeout

    def capture(seconds: float) -> asyncio.Timeout:
        assert seconds == 30
        context = original(seconds)
        contexts.append(context)
        return context

    monkeypatch.setattr(recovery, "timeout", capture, raising=False)
    return contexts


@pytest.mark.parametrize("phase", ["listing", "prepare_lock", "resource_lock"])
async def test_expiry_before_sync_preserves_facts_and_releases_only_its_own_waiter(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, deadlines: list[asyncio.Timeout],
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    created = await bundle.create()
    rid = str(created["resource_id"])
    owner = bundle.restart()
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.transport.list_resources
    calls = 0

    async def blocked_listing() -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await original()

    monkeypatch.setattr(bundle.transport, "list_resources", blocked_listing)
    before = owner._store.get(rid)
    ledger = (bundle.root / ".boxteam/resources.json").read_bytes()
    first = None
    lock = owner._resource_lock(rid)
    if phase == "prepare_lock":
        first = asyncio.create_task(owner.get_resource(rid))
        await asyncio.wait_for(entered.wait(), 2)
    elif phase == "resource_lock":
        await lock.acquire()
        release.set()
    waiting = asyncio.create_task(owner.get_resource(rid))
    try:
        if phase == "listing":
            await asyncio.wait_for(entered.wait(), 2)
        else:
            # 调用启动后即拥有唯一总预算，不需要进入网络或取得锁。
            await asyncio.sleep(0)
        assert deadlines and not waiting.done()
        deadlines[-1].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError, match="冷恢复等待超过 30 秒"):
            await asyncio.wait_for(waiting, 2)
        assert calls == 1
        if phase == "resource_lock":
            assert lock.locked()
            assert owner._recovery._pending == {rid: before.owner_revision + 1}
            assert owner._store.get(rid).reconciliation_status == "verified"
        else:
            assert owner._store.get(rid) == before
            assert (bundle.root / ".boxteam/resources.json").read_bytes() == ledger
            assert owner._recovery._pending is None
        if first is not None:
            assert not first.done()
    finally:
        release.set()
        if phase == "resource_lock":
            lock.release()
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        if first is not None:
            await first
    current = await owner.get_resource(rid)
    assert current["owner_revision"] == before.owner_revision + 1
    assert current["reconciliation_status"] == "verified"
    assert bundle.transport.creates == 1 and not bundle.transport.deleted


async def test_one_deadline_spans_listing_and_sync_and_retry_keeps_the_frozen_revision(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, deadlines: list[asyncio.Timeout],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = [await bundle.create(), await bundle.create()]
    rid, healthy = (str(resource["resource_id"]) for resource in resources)
    owner = bundle.owner
    assert owner is not None

    async def lost_receipt(*_arguments: object, **_options: object) -> dict[str, object]:
        raise ConnectionError("操作已发送但未收到回执")

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "write_terminal" if isinstance(bundle, TerminalOwnerFixture) else "perform", lost_receipt)
        async with owner.operation(
            session_id=SESSION_A, thread_id=THREAD_A,
            holder_id="request:lost-receipt", operation_id="lost-receipt",
        ) as operation:
            with pytest.raises(ConnectionError):
                if isinstance(bundle, TerminalOwnerFixture):
                    await operation.write_terminal(terminal_id=rid, data="pending")
                else:
                    await operation.perform(rid, "read")
    original_lease, = bundle.leases.leases_for_resource(rid)
    assert original_lease.status == "reconcile_required"
    owner = bundle.restart()
    listed, release_listing = asyncio.Event(), asyncio.Event()
    deadlines.clear()
    entered, peer_finished, release_sync = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_list = bundle.transport.list_resources
    original_sync = bundle.transport.sync_owner_revision
    revisions: list[tuple[str, int]] = []

    async def listing() -> list[dict[str, object]]:
        listed.set()
        await release_listing.wait()
        return await original_list()

    async def syncing(resource_id: str, revision: int, *, for_stop: bool) -> None:
        revisions.append((resource_id, revision))
        await original_sync(resource_id, revision, for_stop=for_stop)
        if resource_id == rid:
            entered.set()
            await release_sync.wait()
        else:
            peer_finished.set()

    monkeypatch.setattr(bundle.transport, "list_resources", listing)
    monkeypatch.setattr(bundle.transport, "sync_owner_revision", syncing)
    waiting = asyncio.create_task(owner.recover())
    try:
        await asyncio.wait_for(listed.wait(), 2)
        assert len(deadlines) == 1
        original_deadline = deadlines[0].when()
        release_listing.set()
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(peer_finished.wait(), 2)
        assert len(deadlines) == 1 and deadlines[0].when() == original_deadline
        deadlines[0].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError, match="未确认外部操作停止"):
            await asyncio.wait_for(waiting, 2)
        assert owner._recovery._pending == {rid: 2}
        assert owner._store.get(rid).reconciliation_status == "unknown"
        assert owner._store.get(healthy).reconciliation_status == "verified"
        assert bundle.leases.leases_for_resource(rid)[0].lease_id == original_lease.lease_id
        assert bundle.leases.leases_for_resource(rid)[0].status == "reconcile_required"
        assert all(not lock.locked() for lock in owner._resource_locks.values())
        assert revisions == [(rid, 2), (healthy, 2)]
    finally:
        release_listing.set()
        release_sync.set()
        if not waiting.done():
            waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    await owner.recover()
    assert owner._recovery._pending == {}
    assert revisions == [(rid, 2), (healthy, 2), (rid, 2)]
    assert owner._store.get(rid).reconciliation_status == "unknown"
    assert bundle.leases.leases_for_resource(rid)[0].lease_id == original_lease.lease_id
    assert bundle.leases.leases_for_resource(rid)[0].status == "reconcile_required"
    assert (await owner.get_resource(healthy))["owner_revision"] == 2
    assert bundle.transport.creates == 2 and not bundle.transport.deleted


async def test_transport_timeout_is_preserved_without_becoming_a_budget_expiry(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, deadlines: list[asyncio.Timeout],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await bundle.create()
    owner = bundle.restart()
    failure = TimeoutError("原transport读取超时")

    async def failed_listing() -> list[dict[str, object]]:
        raise failure

    monkeypatch.setattr(bundle.transport, "list_resources", failed_listing)
    with pytest.raises(TimeoutError) as caught:
        await owner.get_resource(str(created["resource_id"]))
    assert caught.value is failure
    if deadlines:
        assert not deadlines[-1].expired()
