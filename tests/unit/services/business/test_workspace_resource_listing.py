"""列表网络等待不能占住资源锁，迟到清单不能覆盖已提交变化。"""

from __future__ import annotations

import asyncio

import pytest

from app.services.business.workspace_resources.owner import ResourceConflict
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.terminal import TerminalOwnerFixture
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
