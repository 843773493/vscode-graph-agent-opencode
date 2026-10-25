"""列表网络等待不能占住资源锁，迟到清单不能覆盖已提交变化。"""

from __future__ import annotations

import asyncio

import pytest

from app.services.business.workspace_resources.owner import ResourceConflict
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.test_workspace_resource_command_deadline import (
    advance_clock as advance_clock,  # noqa: PLC0414
)
from tests.unit.services.business.test_workspace_resource_locking import (
    bundle as bundle,  # noqa: PLC0414
)


async def test_slow_list_keeps_independent_queries_available(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = bundle.owner
    first, peer = await bundle.create(), await bundle.create()
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.transport.list_resources
    held = False

    async def list_receipt() -> list[dict[str, object]]:
        nonlocal held
        receipt = await original()
        if not held:
            held = True
            entered.set()
            await release.wait()
        return receipt

    monkeypatch.setattr(bundle.transport, "list_resources", list_receipt)
    listing = asyncio.create_task(owner.list_resources())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        current = await asyncio.wait_for(owner.get_resource(str(peer["resource_id"])), 2)
        assert current["owner_revision"] == 1 and current["reconciliation_status"] == "verified"
        assert not listing.done()
        release.set()
        result = await asyncio.wait_for(listing, 2)
        assert {item["resource_id"] for item in result} == {first["resource_id"], peer["resource_id"]}
        assert all(item["status"] == "running" and item["owner_revision"] == 1 for item in result)
    finally:
        release.set()
        await asyncio.gather(listing, return_exceptions=True)


@pytest.mark.parametrize("action", ["promote", "stop"])
async def test_late_list_cannot_replace_a_committed_command(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch, action: str,
) -> None:
    owner = bundle.owner
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.transport.list_resources

    async def list_receipt() -> list[dict[str, object]]:
        receipt = await original()
        entered.set()
        await release.wait()
        return receipt

    monkeypatch.setattr(bundle.transport, "list_resources", list_receipt)
    listing = asyncio.create_task(owner.list_resources())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        committed = await asyncio.wait_for(bundle.command(rid, action=action, expected_revision=1), 2)
        before = owner._store.path.read_bytes()
        ledger = bundle.leases._state_path.read_bytes()
        release.set()
        with pytest.raises(ResourceConflict, match="列表读取期间状态已改变"):
            await asyncio.wait_for(listing, 2)
        assert owner._store.path.read_bytes() == before
        assert bundle.leases._state_path.read_bytes() == ledger
        assert committed["owner_revision"] == owner._store.get(rid).owner_revision == 2
        assert owner._store.get(rid).status == committed["status"]
        assert bundle.leases.leases_for_resource(rid) == []
    finally:
        release.set()
        await asyncio.gather(listing, return_exceptions=True)


async def test_late_list_cannot_replace_a_same_revision_external_observation(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = bundle.owner
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.transport.list_resources
    held = False

    async def list_receipt() -> list[dict[str, object]]:
        nonlocal held
        receipt = await original()
        if not held:
            held = True
            entered.set()
            await release.wait()
        return receipt

    monkeypatch.setattr(bundle.transport, "list_resources", list_receipt)
    listing = asyncio.create_task(owner.list_resources())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # 外部退出不推进Python owner版本，真实查询负责观察并持久发布终态。
        await bundle.transport.stop_resource(rid)
        observed = await asyncio.wait_for(owner.get_resource(rid), 2)
        assert observed["owner_revision"] == 1
        assert observed["status"] == ("terminated" if isinstance(bundle, TerminalOwnerFixture) else "closed")
        before = owner._store.path.read_bytes()
        ledger = bundle.leases._state_path.read_bytes()
        release.set()
        with pytest.raises(ResourceConflict, match="列表读取期间状态已改变"):
            await asyncio.wait_for(listing, 2)
        assert owner._store.path.read_bytes() == before
        assert bundle.leases._state_path.read_bytes() == ledger
        assert owner._store.get(rid).status == observed["status"]
    finally:
        release.set()
        await asyncio.gather(listing, return_exceptions=True)


async def test_cancelled_list_preserves_records_and_independent_commands(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = bundle.owner
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    entered = asyncio.Event()
    original = bundle.transport.list_resources
    before = owner._store.path.read_bytes()
    ledger = bundle.leases._state_path.read_bytes()

    async def list_receipt() -> list[dict[str, object]]:
        await original()
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("已取消的清单不能返回")

    monkeypatch.setattr(bundle.transport, "list_resources", list_receipt)
    listing = asyncio.create_task(owner.list_resources())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        listing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listing
        assert owner._store.path.read_bytes() == before
        assert bundle.leases._state_path.read_bytes() == ledger
        result = await asyncio.wait_for(bundle.command(rid, action="stop", expected_revision=1), 2)
        assert result["owner_revision"] == 2 and result["reconciliation_status"] == "verified"
    finally:
        listing.cancel()
        await asyncio.gather(listing, return_exceptions=True)


@pytest.mark.parametrize("stage", ["prepare", "recovery", "receipt", "lock"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_list_total_deadline_or_cancel_drains_only_its_waiter(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    advance_clock, stage: str, cancel: bool,
) -> None:
    owner = bundle.owner
    await bundle.create()
    await bundle.create()
    rid, peer = (record.resource_id for record in owner._store.all())
    before = owner._store.path.read_bytes()
    ledger = bundle.leases._state_path.read_bytes()
    recover, transport_list = owner.recover, bundle.transport.list_resources
    entered, release = asyncio.Event(), asyncio.Event()
    lock = owner._resource_lock(rid)
    acquired = False

    def is_listing() -> bool:
        return asyncio.current_task().get_name() == "list-under-test"

    async def recovering(resource_ids=None):
        selected = tuple(resource_ids) if resource_ids is not None else None
        if is_listing() and ((stage == "prepare" and selected == ())
                             or (stage == "recovery" and selected)):
            entered.set()
            await release.wait()
        await recover(selected)

    async def receiving():
        nonlocal acquired
        result = await transport_list()
        if is_listing() and stage in {"receipt", "lock"}:
            if stage == "lock":
                await lock.acquire()
                acquired = True
            entered.set()
            if stage == "receipt":
                await release.wait()
        return result

    monkeypatch.setattr(owner, "recover", recovering)
    monkeypatch.setattr(bundle.transport, "list_resources", receiving)
    pending = asyncio.create_task(owner.list_resources(), name="list-under-test")
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            pending.cancel("caller-cancelled-list")
            with pytest.raises(asyncio.CancelledError, match="caller-cancelled-list"):
                await pending
        else:
            advance_clock(31)
            with pytest.raises(TimeoutError, match="列表等待超过 30 秒") as caught:
                await asyncio.wait_for(asyncio.shield(pending), 1)
            assert owner.workspace_id in str(caught.value)
            assert str(bundle.root) not in str(caught.value)
        assert owner._store.path.read_bytes() == before
        assert bundle.leases._state_path.read_bytes() == ledger
        healthy = await asyncio.wait_for(owner.get_resource(peer), 2)
        assert healthy["status"] == "running" and healthy["owner_revision"] == 1
        assert not bundle.transport.deleted
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        release.set()
        if acquired:
            lock.release()
    await asyncio.sleep(0)
    assert owner._store.get(rid).status == "running"
    assert not bundle.leases.leases_for_resource(rid)


async def test_list_recovery_and_receipt_share_one_deadline(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    advance_clock,
) -> None:
    owner = bundle.owner
    await bundle.create()
    recover = owner.recover
    entered, release = asyncio.Event(), asyncio.Event()
    before = owner._store.path.read_bytes()
    calls = 0

    async def recovering(resource_ids=None):
        nonlocal calls
        await recover(resource_ids)
        calls += 1
        if calls == 1:
            advance_clock(20)

    async def receiving():
        entered.set()
        await release.wait()
        raise AssertionError("到期后不应返回清单")

    monkeypatch.setattr(owner, "recover", recovering)
    monkeypatch.setattr(bundle.transport, "list_resources", receiving)
    pending = asyncio.create_task(owner.list_resources())
    try:
        await asyncio.wait_for(entered.wait(), 60)
        assert calls == 2
        advance_clock(11)
        with pytest.raises(TimeoutError, match="列表等待超过 30 秒"):
            await asyncio.wait_for(asyncio.shield(pending), 1)
        assert owner._store.path.read_bytes() == before
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        release.set()


async def test_list_preserves_the_transport_own_timeout(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await bundle.create()
    failure = TimeoutError("原清单传输超时")

    async def failed_list():
        raise failure

    monkeypatch.setattr(bundle.transport, "list_resources", failed_list)
    with pytest.raises(TimeoutError) as caught:
        await bundle.owner.list_resources()
    assert caught.value is failure


async def test_list_deadline_retains_already_committed_observations(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    advance_clock,
) -> None:
    owner = bundle.owner
    await bundle.create()
    await bundle.create()
    first, last = owner._store.all()
    await bundle.transport.stop_resource(first.resource_id)
    published = asyncio.Event()
    save = owner._store.save
    transport_list = bundle.transport.list_resources
    lock = owner._resource_lock(last.resource_id)
    acquired = False

    def saving(*records):
        save(*records)
        if any(record.resource_id == first.resource_id for record in records):
            published.set()

    monkeypatch.setattr(owner._store, "save", saving)

    async def receiving():
        nonlocal acquired
        result = await transport_list()
        # 完成原冷恢复后才占锁，只挂起清单合并的第二项。
        await lock.acquire()
        acquired = True
        return result

    monkeypatch.setattr(bundle.transport, "list_resources", receiving)
    pending = asyncio.create_task(owner.list_resources())
    try:
        await asyncio.wait_for(published.wait(), 2)
        advance_clock(31)
        with pytest.raises(TimeoutError, match="列表等待超过 30 秒"):
            await asyncio.wait_for(asyncio.shield(pending), 1)
        committed = owner._store.get(first.resource_id)
        assert committed.status == ("terminated" if isinstance(bundle, TerminalOwnerFixture) else "closed")
        assert committed.reconciliation_status == "verified" and committed.owner_revision == first.owner_revision
        assert owner._store.get(last.resource_id) == last
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        if acquired:
            lock.release()
    await asyncio.sleep(0)
    assert owner._store.get(first.resource_id) == committed
    assert owner._store.get(last.resource_id) == last
