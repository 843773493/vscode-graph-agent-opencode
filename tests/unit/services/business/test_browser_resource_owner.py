from __future__ import annotations

import asyncio
import json
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
