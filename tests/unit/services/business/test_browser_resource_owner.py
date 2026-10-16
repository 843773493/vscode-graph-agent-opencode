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
from tests.support.browser_owner import BrowserBundle, build_browser_bundle
from tests.support.terminal_owner import CHILD, SESSION_A, SESSION_B, THREAD_A, THREAD_B


@pytest.fixture
def bundle(tmp_path: Path) -> BrowserBundle:
    return build_browser_bundle(tmp_path)


@pytest.mark.asyncio
async def test_precise_thread_filter_and_unload_remove_detached_temporary_browser(bundle: BrowserBundle) -> None:
    main = await bundle.create()
    child = await bundle.create(thread=CHILD)
    await bundle.command(main["resource_id"], action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
    assert [item["resource_id"] for item in await bundle.owner.list_resources(thread_id=THREAD_A)] == [main["resource_id"]]
    assert [item["resource_id"] for item in await bundle.owner.list_resources(session_id=SESSION_A, thread_id=CHILD)] == [child["resource_id"]]
    await bundle.owner.release_thread(SESSION_A, THREAD_A, 1)
    await bundle.owner.release_thread(SESSION_A, THREAD_A, 1)
    assert bundle.transport.deleted == [main["resource_id"]]


@pytest.mark.asyncio
async def test_delete_is_unavailable_until_thread_association_is_detached(bundle: BrowserBundle) -> None:
    browser = await bundle.create()
    rid = str(browser["resource_id"])
    ref = ThreadRef(SESSION_A, THREAD_A)
    assert "delete" not in browser["available_actions"]

    with pytest.raises(ResourceConflict, match="仍有关联"):
        await bundle.command(rid, action="delete", expected_revision=1)

    associated = await bundle.owner.get_resource(rid)
    assert associated["thread_associations"] == [{"session_id": SESSION_A, "thread_id": THREAD_A}]
    assert associated["owner_revision"] == 1
    assert associated["status"] == "running"
    assert bundle.transport.records[rid]["transport_owner_revision"] == 1
    assert bundle.transport.deleted == []
    assert bundle.leases.leases_for_resource(rid) == []

    detached = await bundle.command(rid, action="detach", expected_revision=1, thread=ref)
    assert detached["thread_associations"] == []
    assert "delete" in detached["available_actions"]
    deleted = await bundle.command(rid, action="delete", expected_revision=2)
    assert deleted["status"] == "deleted"
    assert bundle.transport.deleted == [rid]


@pytest.mark.asyncio
async def test_workspace_browser_keeps_identity_after_session_cleanup_and_new_association(bundle: BrowserBundle) -> None:
    browser = await bundle.create(scope="workspace")
    source = browser["source_thread"]
    await bundle.command(browser["resource_id"], action="attach", expected_revision=1, thread=ThreadRef(SESSION_B, THREAD_B))
    await bundle.owner.cleanup_session(SESSION_A)
    current = await bundle.owner.get_resource(browser["resource_id"])
    assert current["source_thread"] == source
    assert current["thread_associations"] == [{"session_id": SESSION_B, "thread_id": THREAD_B}]
    await bundle.owner.cleanup_session(SESSION_B)
    bundle.now += timedelta(days=1)
    assert await bundle.owner.sweep_orphans() == []
    assert len(await bundle.owner.list_resources()) == 1


@pytest.mark.asyncio
async def test_orphan_deadline_survives_restart_and_2959_reattach_then_3000_reclaims(bundle: BrowserBundle) -> None:
    browser = await bundle.create()
    rid = browser["resource_id"]
    ref = ThreadRef(SESSION_A, THREAD_A)
    await bundle.command(rid, action="detach", expected_revision=1, thread=ref)
    deadline = (await bundle.owner.get_resource(rid))["reclaim_at"]
    bundle.restart()
    recovered = await bundle.owner.get_resource(rid)
    assert recovered["reclaim_at"] == deadline
    bundle.now += timedelta(seconds=1799)
    assert await bundle.owner.sweep_orphans() == []
    attached = await bundle.command(rid, action="attach", expected_revision=recovered["owner_revision"], thread=ref)
    assert attached["reclaim_at"] is None
    await bundle.command(rid, action="detach", expected_revision=attached["owner_revision"], thread=ref)
    bundle.now += timedelta(seconds=1800)
    assert (await bundle.owner.sweep_orphans())[0]["status"] == "deleted"
    assert bundle.transport.creates == 1


@pytest.mark.asyncio
async def test_create_read_use_one_real_lease_and_block_concurrent_detach(bundle: BrowserBundle, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    perform = bundle.transport.perform

    async def checked(rid: str, action: str, payload: dict[str, object], *, actor: str) -> dict[str, object]:
        leases = bundle.leases.leases_for_resource(rid)
        assert len(leases) == 1
        seen.append(leases[0].lease_id)
        return await perform(rid, action, payload, actor=actor)

    monkeypatch.setattr(bundle.transport, "perform", checked)
    async with bundle.owner.operation(session_id=SESSION_A, thread_id=THREAD_A, holder_id="turn-real", operation_id="call-real") as operation:
        browser = await operation.create_browser(url="about:blank")
        lease = bundle.leases.leases_for_resource(browser["resource_id"])[0].lease_id
        with pytest.raises(ResourceConflict, match="lease"):
            await bundle.command(browser["resource_id"], action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
        await operation.perform(browser["resource_id"], "read")
        assert seen == [lease]
    assert bundle.leases.leases_for_resource(browser["resource_id"]) == []


@pytest.mark.asyncio
async def test_promotion_cancels_due_reclaim_and_version_conflict_is_atomic(bundle: BrowserBundle) -> None:
    browser = await bundle.create()
    await bundle.command(browser["resource_id"], action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
    bundle.now += timedelta(seconds=1800)
    await bundle.command(browser["resource_id"], action="promote", expected_revision=2)
    assert await bundle.owner.sweep_orphans() == []
    with pytest.raises(ResourceConflict, match="版本冲突"):
        await bundle.command(browser["resource_id"], action="attach", expected_revision=2, thread=ThreadRef(SESSION_B, THREAD_B))


@pytest.mark.asyncio
async def test_failed_receipt_keeps_verified_but_lost_receipt_survives_restart_unknown(bundle: BrowserBundle) -> None:
    browser = await bundle.create()
    rid = browser["resource_id"]
    bundle.transport.failure = BrowserRequestError(method="POST", path="/run", status=408, payload={
        "error": "动作已失败", "browser": bundle.transport.records[rid], "operation_outcome": "failed",
    })
    async with bundle.owner.operation(session_id=SESSION_A, thread_id=THREAD_A, holder_id="turn-a", operation_id="call-a") as operation:
        with pytest.raises(BrowserRequestError):
            await operation.perform(rid, "run")
    assert (await bundle.owner.get_resource(rid))["reconciliation_status"] == "verified"
    bundle.transport.failure = ConnectionError("发送后丢失回执")
    async with bundle.owner.operation(session_id=SESSION_A, thread_id=THREAD_A, holder_id="turn-b", operation_id="call-b") as operation:
        with pytest.raises(ConnectionError):
            await operation.perform(rid, "run")
    calls = len(bundle.transport.calls)
    bundle.restart()
    assert (await bundle.owner.get_resource(rid))["reconciliation_status"] == "unknown"
    assert len(bundle.transport.calls) == calls
    assert str(bundle.root) not in (bundle.root / ".boxteam" / "browser-resources.json").read_text()


@pytest.mark.asyncio
async def test_cancelled_call_releases_lease_and_delete_unknown_blocks_session_cleanup(bundle: BrowserBundle, monkeypatch: pytest.MonkeyPatch) -> None:
    browser = await bundle.create()
    started = asyncio.Event()
    async def blocked(*args: object, **kwargs: object) -> dict[str, object]:
        started.set()
        await asyncio.Future()
    monkeypatch.setattr(bundle.transport, "perform", blocked)
    async def call() -> None:
        async with bundle.owner.operation(session_id=SESSION_A, thread_id=THREAD_A, holder_id="turn-c", operation_id="call-c") as operation:
            await operation.perform(browser["resource_id"], "read")
    task = asyncio.create_task(call())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bundle.leases.leases_for_resource(browser["resource_id"]) == []
    saved = json.loads((bundle.root / ".boxteam" / "browser-resources.json").read_text())
    assert saved["records"][0]["reconciliation_status"] == "unknown"
    with pytest.raises(ResourceConflict):
        await bundle.owner.cleanup_session(SESSION_A)


@pytest.fixture
def revision_barrier(
    bundle: BrowserBundle, monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], tuple[asyncio.Event, asyncio.Event]]:
    def arm() -> tuple[asyncio.Event, asyncio.Event]:
        entered, release = asyncio.Event(), asyncio.Event()
        sync = bundle.transport.sync_owner_revision

        async def blocked(resource_id: str, revision: int) -> None:
            # 真实 owner 已持锁并落盘新 revision，传输同步回执在此挂起。
            entered.set()
            await release.wait()
            await sync(resource_id, revision)

        monkeypatch.setattr(bundle.transport, "sync_owner_revision", blocked)
        return entered, release

    return arm


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["promote", "unload"])
async def test_promotion_and_unload_race_at_real_owner_revision_barrier(
    bundle: BrowserBundle,
    revision_barrier: Callable[[], tuple[asyncio.Event, asyncio.Event]],
    first: str,
) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    owner = bundle.owner
    assert owner is not None
    entered, release = revision_barrier()
    first_task = asyncio.create_task(
        bundle.command(rid, action="promote", expected_revision=1)
        if first == "promote"
        else owner.release_thread(SESSION_A, THREAD_A, 1)
    )
    await asyncio.wait_for(entered.wait(), 5)
    second_task = asyncio.create_task(
        owner.release_thread(SESSION_A, THREAD_A, 1)
        if first == "promote"
        else bundle.command(rid, action="promote", expected_revision=1)
    )
    try:
        await asyncio.sleep(0)
        assert owner._lock.locked()
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
        assert bundle.transport.deleted == []
        assert [item["resource_id"] for item in await owner.list_resources()] == [rid]
    else:
        assert isinstance(promoted, ResourceConflict)
        assert "已删除" in str(promoted)
        assert current["retention_scope"] == "thread"
        assert current["status"] == "deleted"
        assert current["owner_revision"] == 2
        assert bundle.transport.deleted == [rid]
        assert await owner.list_resources() == []
    assert bundle.transport.creates == 1
    assert bundle.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["attach", "reclaim"])
async def test_reattach_and_due_orphan_reclaim_race_at_real_owner_revision_barrier(
    bundle: BrowserBundle,
    revision_barrier: Callable[[], tuple[asyncio.Event, asyncio.Event]],
    first: str,
) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    ref = ThreadRef(SESSION_A, THREAD_A)
    detached = await bundle.command(rid, action="detach", expected_revision=1, thread=ref)
    bundle.now += timedelta(seconds=1800)
    owner = bundle.owner
    assert owner is not None
    entered, release = revision_barrier()
    first_task = asyncio.create_task(
        bundle.command(rid, action="attach", expected_revision=detached["owner_revision"], thread=ref)
        if first == "attach"
        else owner.sweep_orphans()
    )
    await asyncio.wait_for(entered.wait(), 5)
    second_task = asyncio.create_task(
        owner.sweep_orphans()
        if first == "attach"
        else bundle.command(rid, action="attach", expected_revision=detached["owner_revision"], thread=ref)
    )
    try:
        await asyncio.sleep(0)
        assert owner._lock.locked()
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
        assert bundle.transport.deleted == []
    else:
        assert isinstance(attached, ResourceConflict)
        assert "已删除" in str(attached)
        assert isinstance(reclaimed, list) and len(reclaimed) == 1
        assert reclaimed[0]["status"] == "deleted"
        assert current["thread_associations"] == []
        assert current["status"] == "deleted"
        assert bundle.transport.deleted == [rid]
    assert bundle.transport.creates == 1
    assert bundle.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
async def test_due_orphan_reclaim_waits_for_real_active_lease_then_converges(
    bundle: BrowserBundle,
) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    detached = await bundle.command(
        rid, action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A),
    )
    lease = bundle.leases.acquire_operation(
        resource_id=rid, turn_stream_id="actual-orphan-holder", operation_id="actual-operation",
        holder_session_id=SESSION_A, holder_thread_id=THREAD_A,
    )
    bundle.now += timedelta(seconds=1800)
    owner = bundle.owner
    assert owner is not None
    assert await owner.sweep_orphans() == []
    current = await owner.get_resource(rid)
    assert current["reclaim_at"] == detached["reclaim_at"]
    assert current["thread_associations"] == []
    assert current["status"] == "running"
    assert bundle.leases.get_lease(lease.lease_id).status == "active"
    assert bundle.transport.deleted == []
    bundle.leases.release(lease.lease_id)
    reclaimed = await owner.sweep_orphans()
    assert len(reclaimed) == 1 and reclaimed[0]["status"] == "deleted"
    assert await owner.sweep_orphans() == []
    assert bundle.transport.deleted == [rid]
    assert bundle.transport.creates == 1
    assert bundle.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
async def test_verified_deleted_grant_rejects_before_network_and_keeps_terminal_fact(
    bundle: BrowserBundle, monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = await bundle.create(scope="workspace")
    rid = browser["resource_id"]
    ref = ThreadRef(SESSION_A, THREAD_A)
    detached = await bundle.command(rid, action="detach", expected_revision=1, thread=ref)
    deleted = await bundle.command(rid, action="delete", expected_revision=detached["owner_revision"])
    before = (bundle.root / ".boxteam/browser-resources.json").read_bytes()
    calls: list[str] = []

    async def forbidden(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append("network")
        raise AssertionError("已核实 deleted 不得请求外部 grant")

    monkeypatch.setattr(bundle.transport, "get_resource", forbidden)
    monkeypatch.setattr(bundle.transport, "issue_transport_grant", forbidden)
    with pytest.raises(ResourceConflict):
        await bundle.owner.transport_grant(
            rid, expected_revision=deleted["owner_revision"],
            holder_id="request:late", operation_id="late",
        )
    assert calls == []
    assert bundle.owner._require(rid).status == "deleted"
    assert bundle.owner._require(rid).reconciliation_status == "verified"
    assert (bundle.root / ".boxteam/browser-resources.json").read_bytes() == before
    assert bundle.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
async def test_lost_grant_receipt_still_blocks_resource_as_unknown(
    bundle: BrowserBundle, monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = await bundle.create(scope="workspace")
    rid = browser["resource_id"]

    async def lost(*args: object, **kwargs: object) -> dict[str, object]:
        raise ConnectionError("grant 响应丢失")

    monkeypatch.setattr(bundle.transport, "issue_transport_grant", lost)
    with pytest.raises(ConnectionError, match="响应丢失"):
        await bundle.owner.transport_grant(
            rid, expected_revision=1, holder_id="request:lost-grant", operation_id="lost-grant",
        )
    assert bundle.owner._require(rid).reconciliation_status == "unknown"
    with pytest.raises(ResourceConflict):
        await bundle.owner.transport_grant(
            rid, expected_revision=1, holder_id="request:retry-grant", operation_id="retry-grant",
        )
