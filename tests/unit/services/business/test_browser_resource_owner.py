from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.business.workspace_resources.records import ThreadRef
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import (
    CHILD_THREAD_A,
    SESSION_A,
    SESSION_B,
    THREAD_A,
    THREAD_B,
)


@pytest.fixture
def browser_owner(tmp_path: Path) -> BrowserOwnerFixture:
    return build_browser_owner_fixture(tmp_path)


@pytest.mark.asyncio
async def test_precise_thread_filter_and_unload_remove_detached_temporary_browser(browser_owner: BrowserOwnerFixture) -> None:
    main = await browser_owner.create()
    child = await browser_owner.create(thread=CHILD_THREAD_A)
    await browser_owner.command(main["resource_id"], action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
    assert [item["resource_id"] for item in await browser_owner.owner.list_resources(thread_id=THREAD_A)] == [main["resource_id"]]
    assert [item["resource_id"] for item in await browser_owner.owner.list_resources(session_id=SESSION_A, thread_id=CHILD_THREAD_A)] == [child["resource_id"]]
    await browser_owner.owner.release_thread(SESSION_A, THREAD_A, 1)
    await browser_owner.owner.release_thread(SESSION_A, THREAD_A, 1)
    assert browser_owner.transport.deleted == [main["resource_id"]]


@pytest.mark.asyncio
async def test_delete_is_unavailable_until_thread_association_is_detached(browser_owner: BrowserOwnerFixture) -> None:
    browser = await browser_owner.create()
    rid = str(browser["resource_id"])
    ref = ThreadRef(SESSION_A, THREAD_A)
    assert "delete" not in browser["available_actions"]

    with pytest.raises(ResourceConflict, match="仍有关联"):
        await browser_owner.command(rid, action="delete", expected_revision=1)

    associated = await browser_owner.owner.get_resource(rid)
    assert associated["thread_associations"] == [{"session_id": SESSION_A, "thread_id": THREAD_A}]
    assert associated["owner_revision"] == 1
    assert associated["status"] == "running"
    assert browser_owner.transport.records[rid]["transport_owner_revision"] == 1
    assert browser_owner.transport.deleted == []
    assert browser_owner.leases.leases_for_resource(rid) == []

    detached = await browser_owner.command(rid, action="detach", expected_revision=1, thread=ref)
    assert detached["thread_associations"] == []
    assert "delete" in detached["available_actions"]
    deleted = await browser_owner.command(rid, action="delete", expected_revision=2)
    assert deleted["status"] == "deleted"
    assert browser_owner.transport.deleted == [rid]


@pytest.mark.asyncio
async def test_workspace_browser_keeps_identity_after_session_cleanup_and_new_association(browser_owner: BrowserOwnerFixture) -> None:
    browser = await browser_owner.create(scope="workspace")
    source = browser["source_thread"]
    await browser_owner.command(browser["resource_id"], action="attach", expected_revision=1, thread=ThreadRef(SESSION_B, THREAD_B))
    await browser_owner.owner.cleanup_session(SESSION_A)
    current = await browser_owner.owner.get_resource(browser["resource_id"])
    assert current["source_thread"] == source
    assert current["thread_associations"] == [{"session_id": SESSION_B, "thread_id": THREAD_B}]
    await browser_owner.owner.cleanup_session(SESSION_B)
    browser_owner.now += timedelta(days=1)
    assert await browser_owner.owner.sweep_orphans() == []
    assert len(await browser_owner.owner.list_resources()) == 1


@pytest.mark.asyncio
async def test_orphan_deadline_survives_restart_and_2959_reattach_then_3000_reclaims(browser_owner: BrowserOwnerFixture) -> None:
    browser = await browser_owner.create()
    rid = browser["resource_id"]
    ref = ThreadRef(SESSION_A, THREAD_A)
    await browser_owner.command(rid, action="detach", expected_revision=1, thread=ref)
    deadline = (await browser_owner.owner.get_resource(rid))["reclaim_at"]
    browser_owner.restart()
    recovered = await browser_owner.owner.get_resource(rid)
    assert recovered["reclaim_at"] == deadline
    browser_owner.now += timedelta(seconds=1799)
    assert await browser_owner.owner.sweep_orphans() == []
    attached = await browser_owner.command(rid, action="attach", expected_revision=recovered["owner_revision"], thread=ref)
    assert attached["reclaim_at"] is None
    await browser_owner.command(rid, action="detach", expected_revision=attached["owner_revision"], thread=ref)
    browser_owner.now += timedelta(seconds=1800)
    assert (await browser_owner.owner.sweep_orphans())[0]["status"] == "deleted"
    assert browser_owner.transport.creates == 1


@pytest.mark.asyncio
async def test_create_read_use_one_real_lease_and_block_concurrent_detach(browser_owner: BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    perform = browser_owner.transport.perform

    async def checked(rid: str, action: str, payload: dict[str, object], *, actor: str) -> dict[str, object]:
        leases = browser_owner.leases.leases_for_resource(rid)
        assert len(leases) == 1
        seen.append(leases[0].lease_id)
        return await perform(rid, action, payload, actor=actor)

    monkeypatch.setattr(browser_owner.transport, "perform", checked)
    async with browser_owner.owner.operation(session_id=SESSION_A, thread_id=THREAD_A, holder_id="turn-real", operation_id="call-real") as operation:
        browser = await operation.create_browser(url="about:blank")
        lease = browser_owner.leases.leases_for_resource(browser["resource_id"])[0].lease_id
        with pytest.raises(ResourceConflict, match="lease"):
            await browser_owner.command(browser["resource_id"], action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
        await operation.perform(browser["resource_id"], "read")
        assert seen == [lease]
    assert browser_owner.leases.leases_for_resource(browser["resource_id"]) == []


@pytest.mark.asyncio
async def test_promotion_cancels_due_reclaim_and_version_conflict_is_atomic(browser_owner: BrowserOwnerFixture) -> None:
    browser = await browser_owner.create()
    await browser_owner.command(browser["resource_id"], action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
    browser_owner.now += timedelta(seconds=1800)
    await browser_owner.command(browser["resource_id"], action="promote", expected_revision=2)
    assert await browser_owner.owner.sweep_orphans() == []
    with pytest.raises(ResourceConflict, match="版本冲突"):
        await browser_owner.command(browser["resource_id"], action="attach", expected_revision=2, thread=ThreadRef(SESSION_B, THREAD_B))


@pytest.mark.asyncio
async def test_failed_receipt_releases_lease_but_lost_receipt_survives_restart(browser_owner: BrowserOwnerFixture) -> None:
    browser = await browser_owner.create()
    rid = browser["resource_id"]
    browser_owner.transport.failure = BrowserRequestError(method="POST", path="/run", status=408, payload={
        "error": "动作已失败", "browser": browser_owner.transport.records[rid], "operation_outcome": "failed",
    })
    async with browser_owner.owner.operation(session_id=SESSION_A, thread_id=THREAD_A, holder_id="turn-a", operation_id="call-a") as operation:
        with pytest.raises(BrowserRequestError):
            await operation.perform(rid, "run")
    assert (await browser_owner.owner.get_resource(rid))["reconciliation_status"] == "verified"
    assert browser_owner.leases.leases_for_resource(rid) == []
    browser_owner.transport.failure = ConnectionError("发送后丢失回执")
    async with browser_owner.owner.operation(session_id=SESSION_A, thread_id=THREAD_A, holder_id="turn-b", operation_id="call-b") as operation:
        with pytest.raises(ConnectionError):
            await operation.perform(rid, "run")
    calls = len(browser_owner.transport.calls)
    browser_owner.restart()
    assert (await browser_owner.owner.get_resource(rid))["reconciliation_status"] == "unknown"
    assert len(browser_owner.transport.calls) == calls
    assert str(browser_owner.root) not in (browser_owner.root / ".boxteam" / "browser-resources.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cancelled_call_retains_lease_and_delete_unknown_blocks_session_cleanup(browser_owner: BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    browser = await browser_owner.create()
    started = asyncio.Event()
    async def blocked(*args: object, **kwargs: object) -> dict[str, object]:
        started.set()
        await asyncio.Future()
    monkeypatch.setattr(browser_owner.transport, "perform", blocked)
    async def call() -> None:
        async with browser_owner.owner.operation(session_id=SESSION_A, thread_id=THREAD_A, holder_id="turn-c", operation_id="call-c") as operation:
            await operation.perform(browser["resource_id"], "read")
    task = asyncio.create_task(call())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pending, = browser_owner.leases.leases_for_resource(browser["resource_id"])
    assert pending.status == "reconcile_required"
    assert (pending.turn_stream_id, pending.operation_id) == ("turn-c", "call-c")
    saved = json.loads((browser_owner.root / ".boxteam" / "browser-resources.json").read_text())
    assert saved["records"][0]["reconciliation_status"] == "unknown"
    with pytest.raises(ResourceConflict):
        await browser_owner.owner.cleanup_session(SESSION_A)


@pytest.fixture
def revision_barrier(
    browser_owner: BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], tuple[asyncio.Event, asyncio.Event]]:
    def arm() -> tuple[asyncio.Event, asyncio.Event]:
        entered, release = asyncio.Event(), asyncio.Event()
        sync = browser_owner.transport.sync_owner_revision

        async def blocked(resource_id: str, revision: int, *, for_stop: bool) -> None:
            # 真实 owner 已持锁并落盘新 revision，传输同步回执在此挂起。
            entered.set()
            await release.wait()
            await sync(resource_id, revision, for_stop=for_stop)

        monkeypatch.setattr(browser_owner.transport, "sync_owner_revision", blocked)
        return entered, release

    return arm


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["promote", "unload"])
async def test_promotion_and_unload_race_at_real_owner_revision_barrier(
    browser_owner: BrowserOwnerFixture,
    revision_barrier: Callable[[], tuple[asyncio.Event, asyncio.Event]],
    first: str,
) -> None:
    resource = await browser_owner.create()
    rid = str(resource["resource_id"])
    owner = browser_owner.owner
    assert owner is not None
    entered, release = revision_barrier()
    first_task = asyncio.create_task(
        browser_owner.command(rid, action="promote", expected_revision=1)
        if first == "promote"
        else owner.release_thread(SESSION_A, THREAD_A, 1)
    )
    await asyncio.wait_for(entered.wait(), 5)
    second_task = asyncio.create_task(
        owner.release_thread(SESSION_A, THREAD_A, 1)
        if first == "promote"
        else browser_owner.command(rid, action="promote", expected_revision=1)
    )
    try:
        await asyncio.sleep(0)
        assert owner._resource_lock(rid).locked()
        assert not second_task.done()
    finally:
        release.set()
        outcomes = await asyncio.gather(first_task, second_task, return_exceptions=True)
    promoted = outcomes[0] if first == "promote" else outcomes[1]
    unloaded = outcomes[1] if first == "promote" else outcomes[0]
    assert unloaded is None
    current = await owner.get_resource(rid)
    assert current["source_thread"] == resource["source_thread"]
    assert current["thread_associations"] == []
    if first == "promote":
        assert isinstance(promoted, dict)
        assert current["retention_scope"] == "workspace"
        assert current["status"] == "running"
        assert current["owner_revision"] == 3
        assert browser_owner.transport.deleted == []
        assert [item["resource_id"] for item in await owner.list_resources()] == [rid]
    else:
        assert isinstance(promoted, ResourceConflict)
        assert "已删除" in str(promoted)
        assert current["retention_scope"] == "thread"
        assert current["status"] == "deleted"
        assert current["owner_revision"] == 2
        assert browser_owner.transport.deleted == [rid]
        assert await owner.list_resources() == []
    assert browser_owner.transport.creates == 1
    assert browser_owner.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["attach", "reclaim"])
async def test_reattach_and_due_orphan_reclaim_race_at_real_owner_revision_barrier(
    browser_owner: BrowserOwnerFixture,
    revision_barrier: Callable[[], tuple[asyncio.Event, asyncio.Event]],
    first: str,
) -> None:
    resource = await browser_owner.create()
    rid = str(resource["resource_id"])
    ref = ThreadRef(SESSION_A, THREAD_A)
    detached = await browser_owner.command(rid, action="detach", expected_revision=1, thread=ref)
    browser_owner.now += timedelta(seconds=1800)
    owner = browser_owner.owner
    assert owner is not None
    entered, release = revision_barrier()
    first_task = asyncio.create_task(
        browser_owner.command(rid, action="attach", expected_revision=detached["owner_revision"], thread=ref)
        if first == "attach"
        else owner.sweep_orphans()
    )
    await asyncio.wait_for(entered.wait(), 5)
    second_task = asyncio.create_task(
        owner.sweep_orphans()
        if first == "attach"
        else browser_owner.command(rid, action="attach", expected_revision=detached["owner_revision"], thread=ref)
    )
    try:
        await asyncio.sleep(0)
        assert owner._resource_lock(rid).locked()
        assert not second_task.done()
    finally:
        release.set()
        outcomes = await asyncio.gather(first_task, second_task, return_exceptions=True)
    attached = outcomes[0] if first == "attach" else outcomes[1]
    reclaimed = outcomes[1] if first == "attach" else outcomes[0]
    current = await owner.get_resource(rid)
    assert current["source_thread"] == resource["source_thread"]
    assert current["owner_revision"] == 3
    assert current["reclaim_at"] is None
    if first == "attach":
        assert isinstance(attached, dict)
        assert reclaimed == []
        assert current["thread_associations"] == [{"session_id": SESSION_A, "thread_id": THREAD_A}]
        assert current["status"] == "running"
        assert browser_owner.transport.deleted == []
    else:
        assert isinstance(attached, ResourceConflict)
        assert "已删除" in str(attached)
        assert isinstance(reclaimed, list) and len(reclaimed) == 1
        assert reclaimed[0]["status"] == "deleted"
        assert current["thread_associations"] == []
        assert current["status"] == "deleted"
        assert browser_owner.transport.deleted == [rid]
    assert browser_owner.transport.creates == 1
    assert browser_owner.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
async def test_due_orphan_reclaim_waits_for_real_active_lease_then_converges(
    browser_owner: BrowserOwnerFixture,
) -> None:
    resource = await browser_owner.create()
    rid = str(resource["resource_id"])
    detached = await browser_owner.command(
        rid, action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A),
    )
    lease = browser_owner.leases.acquire_operation(
        resource_id=rid, turn_stream_id="actual-orphan-holder", operation_id="actual-operation",
        holder_session_id=SESSION_A, holder_thread_id=THREAD_A,
    )
    browser_owner.now += timedelta(seconds=1800)
    owner = browser_owner.owner
    assert owner is not None
    assert await owner.sweep_orphans() == []
    current = await owner.get_resource(rid)
    assert current["reclaim_at"] == detached["reclaim_at"]
    assert current["thread_associations"] == []
    assert current["status"] == "running"
    assert browser_owner.leases.get_lease(lease.lease_id).status == "active"
    assert browser_owner.transport.deleted == []
    browser_owner.leases.release(lease.lease_id)
    reclaimed = await owner.sweep_orphans()
    assert len(reclaimed) == 1 and reclaimed[0]["status"] == "deleted"
    assert await owner.sweep_orphans() == []
    assert browser_owner.transport.deleted == [rid]
    assert browser_owner.transport.creates == 1
    assert browser_owner.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
async def test_verified_deleted_grant_rejects_before_network_and_keeps_terminal_fact(
    browser_owner: BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = await browser_owner.create(scope="workspace")
    rid = browser["resource_id"]
    ref = ThreadRef(SESSION_A, THREAD_A)
    detached = await browser_owner.command(rid, action="detach", expected_revision=1, thread=ref)
    deleted = await browser_owner.command(rid, action="delete", expected_revision=detached["owner_revision"])
    before = (browser_owner.root / ".boxteam/browser-resources.json").read_bytes()
    calls: list[str] = []

    async def forbidden(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append("network")
        raise AssertionError("已核实 deleted 不得请求外部 grant")

    monkeypatch.setattr(browser_owner.transport, "get_resource", forbidden)
    monkeypatch.setattr(browser_owner.transport, "issue_transport_grant", forbidden)
    with pytest.raises(ResourceConflict):
        await browser_owner.owner.transport_grant(
            rid, expected_revision=deleted["owner_revision"],
            holder_id="request:late", operation_id="late",
        )
    assert calls == []
    assert browser_owner.owner._require_owned_resource_record(rid).status == "deleted"
    assert browser_owner.owner._require_owned_resource_record(rid).reconciliation_status == "verified"
    assert (browser_owner.root / ".boxteam/browser-resources.json").read_bytes() == before
    assert browser_owner.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
async def test_lost_grant_receipt_still_blocks_resource_as_unknown(
    browser_owner: BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = await browser_owner.create(scope="workspace")
    rid = browser["resource_id"]

    async def lost(*args: object, **kwargs: object) -> dict[str, object]:
        raise ConnectionError("grant 响应丢失")

    monkeypatch.setattr(browser_owner.transport, "issue_transport_grant", lost)
    with pytest.raises(ConnectionError, match="响应丢失"):
        await browser_owner.owner.transport_grant(
            rid, expected_revision=1, holder_id="request:lost-grant", operation_id="lost-grant",
        )
    assert browser_owner.owner._require_owned_resource_record(rid).reconciliation_status == "unknown"
    with pytest.raises(ResourceConflict):
        await browser_owner.owner.transport_grant(
            rid, expected_revision=1, holder_id="request:retry-grant", operation_id="retry-grant",
        )
