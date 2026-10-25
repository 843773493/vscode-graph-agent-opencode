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


@pytest.mark.parametrize("failure_count", [1, 2])
async def test_cold_recovery_finishes_other_resources_without_rebinding_the_batch_after_failure(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture,
    monkeypatch: pytest.MonkeyPatch, failure_count: int,
) -> None:
    resources = [await bundle.create() for _ in range(3)]
    ids = [str(resource["resource_id"]) for resource in resources]
    owner = bundle.restart()
    errors = {rid: ConnectionError(f"撤权回执丢失: {rid}") for rid in ids[:failure_count]}
    calls: list[tuple[str, int]] = []
    original = bundle.transport.sync_owner_revision

    async def lose_receipt(resource_id: str, revision: int) -> None:
        calls.append((resource_id, revision))
        # 外部版本已经发布；Python不能因丢失回执重启整批恢复。
        await original(resource_id, revision)
        if resource_id in errors:
            raise errors[resource_id]

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", lose_receipt)
    with pytest.raises(ConnectionError if failure_count == 1 else ExceptionGroup) as caught:
        await owner.recover()
    if failure_count == 1:
        assert caught.value is errors[ids[0]]
    else:
        assert isinstance(caught.value, ExceptionGroup)
        assert caught.value.exceptions == tuple(errors.values())
    assert calls == [(rid, 2) for rid in ids]

    # 健康资源可经原查询、授权和工具入口继续使用，版本不会反复推进。
    healthy = ids[-1]
    current = await owner.get_resource(healthy)
    assert current["owner_revision"] == 2
    assert current["reconciliation_status"] == "verified"
    grant = await owner.transport_grant(
        healthy, expected_revision=2, thread=ThreadRef(SESSION_A, THREAD_A),
        holder_id="request:recovered-grant", operation_id="recovered-grant",
    )
    assert grant["resource"]["resource_id"] == healthy
    async with owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A,
        holder_id="request:recovered-tool", operation_id="recovered-tool",
    ) as operation:
        if isinstance(bundle, TerminalOwnerFixture):
            await operation.write_terminal(terminal_id=healthy, data="recovered")
        else:
            await operation.perform(healthy, "navigate", {"url": "https://recovered.test"})
    for rid in errors:
        unknown = await owner.get_resource(rid)
        assert unknown["owner_revision"] == 2
        assert unknown["reconciliation_status"] == "unknown"
        with pytest.raises(ResourceConflict, match="尚待 owner 核实"):
            await bundle.command(rid, action="promote", expected_revision=2)
    assert calls == [(rid, 2) for rid in ids]
    assert all(record.owner_revision == 2 for record in owner._store.records.values())
    assert not bundle.transport.deleted


async def test_cancelling_cold_recovery_does_not_report_the_remaining_resources_recovered(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = await bundle.create(), await bundle.create()
    ids = [str(first["resource_id"]), str(second["resource_id"])]
    owner = bundle.restart()
    entered = asyncio.Event()
    calls: list[str] = []

    async def blocked(resource_id: str, _revision: int) -> None:
        calls.append(resource_id)
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", blocked)
    recovering = asyncio.create_task(owner.recover())
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        recovering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovering
    assert calls == ids
    assert set(owner._recovery._pending) == set(ids)
    assert all(owner._store.records[rid].reconciliation_status == "unknown" for rid in ids)


async def test_pending_cold_recovery_does_not_block_healthy_resource_entrypoints(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = await bundle.create(), await bundle.create(thread=CHILD_THREAD_A)
    rid, healthy = str(first["resource_id"]), str(second["resource_id"])
    owner = bundle.restart()
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.transport.sync_owner_revision
    calls: list[tuple[str, int]] = []

    async def pending_revision(resource_id: str, revision: int) -> None:
        calls.append((resource_id, revision))
        if resource_id == rid:
            entered.set()
            await release.wait()
        await original(resource_id, revision)

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", pending_revision)
    recovering = asyncio.create_task(owner.recover())
    waiting = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        current = await asyncio.wait_for(owner.get_resource(healthy), 2)
        assert current["owner_revision"] == 2 and current["reconciliation_status"] == "verified"
        waiting = asyncio.create_task(owner.get_resource(rid))
        await asyncio.sleep(0)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not recovering.done()
        created = await asyncio.wait_for(bundle.create(thread=CHILD_THREAD_A), 2)
        listed = await asyncio.wait_for(owner.list_resources(thread_id=CHILD_THREAD_A), 2)
        assert {item["resource_id"] for item in listed} == {healthy, created["resource_id"]}
        grant = await asyncio.wait_for(owner.transport_grant(
            healthy, expected_revision=2, thread=ThreadRef(SESSION_A, CHILD_THREAD_A),
            holder_id="request:cold-grant", operation_id="cold-grant",
        ), 2)
        assert grant["resource"]["resource_id"] == healthy
        async with owner.operation(
            session_id=SESSION_A, thread_id=CHILD_THREAD_A,
            holder_id="request:cold-tool", operation_id="cold-tool",
        ) as operation:
            if isinstance(bundle, TerminalOwnerFixture):
                await asyncio.wait_for(operation.write_terminal(terminal_id=healthy, data="cold-independent"), 2)
            else:
                await asyncio.wait_for(operation.perform(healthy, "navigate", {"url": "https://cold.test"}), 2)
        await asyncio.wait_for(owner.release_thread(SESSION_A, CHILD_THREAD_A, 1), 2)
        assert set(bundle.transport.deleted) == {healthy, created["resource_id"]}
        assert not recovering.done() and calls.count((rid, 2)) == calls.count((healthy, 2)) == 1
        assert owner._store.records[rid].thread_associations == [ThreadRef(SESSION_A, THREAD_A)]
    finally:
        release.set()
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await recovering
    assert (await owner.get_resource(rid))["owner_revision"] == 2
    assert calls.count((rid, 2)) == calls.count((healthy, 2)) == 1


async def test_cancelling_recovery_keeps_frozen_revision_and_does_not_repeat_finished_resources(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = await bundle.create(), await bundle.create(thread=CHILD_THREAD_A)
    rid, healthy = str(first["resource_id"]), str(second["resource_id"])
    owner = bundle.restart()
    entered, healthy_synced, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = bundle.transport.sync_owner_revision
    calls: list[tuple[str, int]] = []

    async def pending_revision(resource_id: str, revision: int) -> None:
        calls.append((resource_id, revision))
        if resource_id == rid:
            entered.set()
            await release.wait()
        await original(resource_id, revision)
        if resource_id == healthy:
            healthy_synced.set()

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", pending_revision)
    recovering = asyncio.create_task(owner.recover())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(healthy_synced.wait(), 5)
    finally:
        recovering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovering
    assert owner._recovery._pending == {rid: 2}
    assert owner._store.records[rid].reconciliation_status == "unknown"
    healthy_current = await owner.get_resource(healthy)
    assert healthy_current["owner_revision"] == 2 and healthy_current["reconciliation_status"] == "verified"
    assert calls == [(rid, 2), (healthy, 2)]
    release.set()
    unknown = await owner.get_resource(rid)
    assert unknown["owner_revision"] == 2 and unknown["reconciliation_status"] == "unknown"
    assert calls == [(rid, 2), (healthy, 2), (rid, 2)]
    assert owner._recovery._pending == {}
    assert bundle.transport.creates == 2 and not bundle.transport.deleted
