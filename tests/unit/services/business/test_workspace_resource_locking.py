"""两个资源 owner 的锁隔离、过滤重检及迟到调用合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.business.workspace_resources.records import ThreadRef
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import CHILD_THREAD_A, SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)


@pytest.fixture(params=["terminal", "browser"])
def bundle(request: pytest.FixtureRequest, tmp_path: Path) -> TerminalOwnerFixture | BrowserOwnerFixture:
    return (build_terminal_owner_fixture if request.param == "terminal" else build_browser_owner_fixture)(tmp_path)


async def test_pending_stop_does_not_block_another_resource_or_thread_cleanup(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = bundle.owner
    assert owner is not None
    first = await bundle.create()
    second = await bundle.create(thread=CHILD_THREAD_A)
    rid, other = str(first["resource_id"]), str(second["resource_id"])
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.transport.stop_resource
    calls = []

    async def blocked_stop(resource_id: str, *, reason: str | None = None) -> dict[str, object]:
        calls.append(resource_id)
        if resource_id == rid:
            entered.set()
            await release.wait()
        return await original(resource_id, reason=reason)

    monkeypatch.setattr(bundle.transport, "stop_resource", blocked_stop)
    stopping = asyncio.create_task(bundle.command(rid, action="stop", expected_revision=1))
    queued = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        queued = asyncio.create_task(bundle.command(rid, action="promote", expected_revision=1))
        # 同资源命令仍串行；其它资源的创建、查询、授权、工具和释放独立完成。
        await asyncio.sleep(0)
        assert not queued.done()
        current = await asyncio.wait_for(owner.get_resource(other), 5)
        assert current["owner_revision"] == 1 and current["status"] == "running"
        third = await asyncio.wait_for(bundle.create(thread=CHILD_THREAD_A), 5)
        listed = await asyncio.wait_for(owner.list_resources(thread_id=CHILD_THREAD_A), 5)
        assert {item["resource_id"] for item in listed} == {other, third["resource_id"]}
        grant = await asyncio.wait_for(owner.transport_grant(
            other, expected_revision=1, thread=ThreadRef(SESSION_A, CHILD_THREAD_A),
            holder_id="request:independent-grant", operation_id="independent-grant",
        ), 5)
        assert grant["resource"]["resource_id"] == other
        async with owner.operation(session_id=SESSION_A, thread_id=CHILD_THREAD_A,
                                   holder_id="request:independent-tool", operation_id="independent-tool") as operation:
            if isinstance(bundle, TerminalOwnerFixture):
                await asyncio.wait_for(operation.write_terminal(terminal_id=other, data="independent"), 5)
            else:
                await asyncio.wait_for(operation.perform(other, "navigate", {"url": "https://independent.test"}), 5)
        await asyncio.wait_for(owner.release_thread(SESSION_A, CHILD_THREAD_A, 1), 5)
        assert set(bundle.transport.deleted) == {other, third["resource_id"]}
        assert calls == [rid] and not stopping.done() and not queued.done()
        assert owner._store.records[rid].status == "stopping"
        assert owner._store.records[rid].thread_associations == [ThreadRef(SESSION_A, THREAD_A)]
        assert len(bundle.leases.leases_for_resource(rid)) == 1
    finally:
        release.set()
        results = await asyncio.gather(stopping, *([queued] if queued is not None else []), return_exceptions=True)
    assert isinstance(results[0], dict) and results[0]["reconciliation_status"] == "verified"
    assert isinstance(results[1], ResourceConflict)
    assert calls == [rid] and not bundle.leases.leases_for_resource(rid)


async def test_filtered_list_rechecks_association_after_waiting_for_the_resource(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = bundle.owner
    assert owner is not None
    resource = await bundle.create(scope="workspace", thread=CHILD_THREAD_A)
    rid = str(resource["resource_id"])
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.transport.sync_owner_revision

    async def blocked_revision(resource_id: str, owner_revision: int) -> None:
        entered.set()
        await release.wait()
        await original(resource_id, owner_revision)

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", blocked_revision)
    detach = asyncio.create_task(bundle.command(rid, action="detach", expected_revision=1,
                                                thread=ThreadRef(SESSION_A, CHILD_THREAD_A)))
    listing = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        listing = asyncio.create_task(owner.list_resources(thread_id=CHILD_THREAD_A))
        await asyncio.sleep(0)
        assert not listing.done()
    finally:
        release.set()
        results = await asyncio.gather(detach, *([listing] if listing is not None else []))
    assert results[0]["thread_associations"] == [] and results[1] == []


async def test_operation_close_rejects_late_calls_while_its_lease_waits_for_a_lock(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
) -> None:
    owner = bundle.owner
    assert owner is not None
    first, second = await bundle.create(), await bundle.create()
    rid, other = str(first["resource_id"]), str(second["resource_id"])
    async with owner.operation(session_id=SESSION_A, thread_id=THREAD_A,
                               holder_id="request:closing-tool", operation_id="closing-tool") as operation:
        if isinstance(bundle, TerminalOwnerFixture):
            await operation.read_terminal(rid)
        else:
            await operation.perform(rid, "navigate", {"url": "https://before-close.test"})
        async with owner._resource_lock(rid):
            closing = asyncio.create_task(operation.close())
            await asyncio.sleep(0)
            assert not closing.done()
            with pytest.raises(RuntimeError, match="已关闭"):
                if isinstance(bundle, TerminalOwnerFixture):
                    await operation.write_terminal(terminal_id=other, data="late")
                else:
                    await operation.perform(other, "navigate", {"url": "https://late.test"})
            assert len(bundle.leases.leases_for_resource(rid)) == 1
            assert not bundle.leases.leases_for_resource(other)
        await closing
    assert not bundle.leases.leases_for_resource(rid)
