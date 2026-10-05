from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest

from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.business.workspace_resources.records import ThreadRef
from tests.support.terminal_owner import (
    CHILD,
    SESSION_A,
    SESSION_B,
    THREAD_A,
    THREAD_B,
    Bundle,
    build_terminal_bundle,
)


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    return build_terminal_bundle(tmp_path)


@pytest.mark.asyncio
async def test_thread_filters_find_exact_associations_and_source_orphans(bundle: Bundle) -> None:
    main = await bundle.create()
    child = await bundle.create(thread=CHILD)
    await bundle.command(str(main["resource_id"]), action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
    assert [item["resource_id"] for item in await bundle.owner.list_resources(thread_id=THREAD_A)] == [main["resource_id"]]
    assert [item["resource_id"] for item in await bundle.owner.list_resources(session_id=SESSION_A, thread_id=CHILD)] == [child["resource_id"]]
    assert await bundle.owner.list_resources(session_id=SESSION_B, thread_id=THREAD_A) == []
    assert [item["resource_id"] for item in await bundle.owner.list_resources(source_thread_id=THREAD_A)] == [main["resource_id"]]


@pytest.mark.asyncio
async def test_creation_uses_catalog_main_or_real_child_and_one_owner(bundle: Bundle) -> None:
    main = await bundle.create()
    child = await bundle.create(thread=CHILD)
    assert main["source_thread"] == {"session_id": SESSION_A, "thread_id": THREAD_A}
    assert child["source_thread_id"] == CHILD
    assert bundle.leases.leases_for_resource(str(main["resource_id"])) == []
    saved = json.loads((bundle.root / ".boxteam" / "terminal-resources.json").read_text())
    assert str(bundle.root) not in json.dumps(saved)
    assert "thread_associations" not in bundle.transport.records[str(main["resource_id"])]
    with pytest.raises(KeyError, match="Thread"):
        await bundle.create(thread=THREAD_B)
    assert bundle.transport.creates == 2


@pytest.mark.asyncio
async def test_workspace_resource_outlives_source_session_and_supports_cross_session_association(bundle: Bundle) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    resource = await bundle.command(rid, action="promote", expected_revision=1)
    ref_b = ThreadRef(SESSION_B, THREAD_B)
    resource = await bundle.command(rid, action="attach", expected_revision=2, thread=ref_b)
    await bundle.owner.cleanup_session(SESSION_A)
    assert not bundle.transport.deleted
    result = await bundle.owner.get_resource(rid)
    assert result["source_thread"] == resource["source_thread"]
    assert result["thread_associations"] == [{"session_id": SESSION_B, "thread_id": THREAD_B}]
    await bundle.owner.cleanup_session(SESSION_B)
    assert (await bundle.owner.list_resources())[0]["thread_associations"] == []
    bundle.now += timedelta(days=1)
    assert await bundle.owner.sweep_orphans() == []


@pytest.mark.asyncio
async def test_thread_unload_immediately_deletes_detached_temporary_resource(bundle: Bundle) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    await bundle.command(rid, action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
    await bundle.owner.release_thread(SESSION_A, THREAD_A, 1)
    await bundle.owner.release_thread(SESSION_A, THREAD_A, 1)
    assert bundle.transport.deleted == [rid]
    assert await bundle.owner.list_resources() == []


@pytest.mark.asyncio
async def test_lease_blocks_own_detach_but_not_other_thread_and_is_released_on_failure(bundle: Bundle) -> None:
    resource = await bundle.create(scope="workspace")
    rid = str(resource["resource_id"])
    await bundle.command(rid, action="attach", expected_revision=1, thread=ThreadRef(SESSION_B, THREAD_B))
    with pytest.raises(RuntimeError, match="故意失败"):
        async with bundle.owner.operation(session_id=SESSION_A, thread_id=THREAD_A, holder_id="turn-real", operation_id="call-real") as operation:
            await operation.get_resource(rid)
            with pytest.raises(ResourceConflict, match="lease"):
                await bundle.command(rid, action="detach", expected_revision=2, thread=ThreadRef(SESSION_A, THREAD_A))
            await bundle.command(rid, action="detach", expected_revision=2, thread=ThreadRef(SESSION_B, THREAD_B))
            with pytest.raises(ResourceConflict, match="lease"):
                await bundle.command(rid, action="stop", expected_revision=3)
            raise RuntimeError("故意失败")
    assert bundle.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
async def test_orphan_reattach_at_2959_then_restart_and_reclaim_at_3000(bundle: Bundle) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    ref = ThreadRef(SESSION_A, THREAD_A)
    await bundle.command(rid, action="detach", expected_revision=1, thread=ref)
    bundle.now += timedelta(seconds=1799)
    assert await bundle.owner.sweep_orphans() == []
    resource = await bundle.command(rid, action="attach", expected_revision=2, thread=ref)
    assert resource["unattached_since"] is None
    await bundle.command(rid, action="detach", expected_revision=3, thread=ref)
    deadline = (await bundle.owner.get_resource(rid))["reclaim_at"]
    bundle.restart()
    assert (await bundle.owner.get_resource(rid))["reclaim_at"] == deadline
    bundle.now += timedelta(seconds=1800)
    assert (await bundle.owner.sweep_orphans())[0]["status"] == "deleted"
    assert bundle.transport.creates == 1


@pytest.mark.asyncio
async def test_promotion_wins_against_due_reclaim_and_version_conflict_is_atomic(bundle: Bundle) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    await bundle.command(rid, action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
    bundle.now += timedelta(seconds=1800)
    await bundle.command(rid, action="promote", expected_revision=2)
    assert await bundle.owner.sweep_orphans() == []
    with pytest.raises(ResourceConflict, match="版本冲突"):
        await bundle.command(rid, action="attach", expected_revision=2, thread=ThreadRef(SESSION_B, THREAD_B))
    assert (await bundle.owner.get_resource(rid))["thread_associations"] == []


@pytest.mark.asyncio
async def test_unknown_delete_retains_discoverable_record_and_no_false_success(bundle: Bundle) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    await bundle.command(rid, action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
    bundle.transport.fail_delete = True
    with pytest.raises(ConnectionError, match="丢失回执"):
        await bundle.command(rid, action="delete", expected_revision=2)
    result = (await bundle.owner.list_resources())[0]
    assert result["reconciliation_status"] == "unknown"
    assert result["status"] != "deleted"
    assert "丢失回执" in result["error"]
    assert bundle.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
async def test_recovery_marks_only_terminal_lease_unknown_without_replay(bundle: Bundle) -> None:
    resource = await bundle.create(scope="workspace")
    rid = str(resource["resource_id"])
    bundle.leases.register_external(resource_id="browser-sentinel", kind="browser_context")
    browser = bundle.leases.acquire_operation(resource_id="browser-sentinel", turn_stream_id="browser-turn", operation_id="navigate")
    terminal = bundle.leases.acquire_operation(resource_id=rid, turn_stream_id="terminal-turn", operation_id="write", holder_session_id=SESSION_A, holder_thread_id=THREAD_A)
    bundle.restart()
    result = await bundle.owner.get_resource(rid)
    assert result["reconciliation_status"] == "reconcile_required"
    assert bundle.leases.get_lease(terminal.lease_id).status == "reconcile_required"
    assert bundle.leases.get_lease(browser.lease_id).status == "active"
    assert bundle.leases.get("browser-sentinel").status == "running"
    assert bundle.transport.creates == 1


@pytest.mark.asyncio
async def test_operation_cancellation_releases_lease_and_keeps_workspace_pty(bundle: Bundle) -> None:
    resource = await bundle.create(scope="workspace")
    rid = str(resource["resource_id"])
    entered = asyncio.Event()

    async def run() -> None:
        async with bundle.owner.operation(session_id=SESSION_A, thread_id=THREAD_A, holder_id="actual-turn", operation_id="actual-tool") as operation:
            await operation.get_resource(rid)
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bundle.leases.leases_for_resource(rid) == []
    assert (await bundle.owner.get_resource(rid))["status"] == "running"


@pytest.mark.asyncio
async def test_creation_read_and_final_receipt_share_one_continuous_lease(
    bundle: Bundle, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []
    create = bundle.transport.create_terminal
    read = bundle.transport.read_terminal
    delete = bundle.transport.delete_resource

    def record_lease(rid: str) -> None:
        leases = bundle.leases.leases_for_resource(rid)
        assert len(leases) == 1
        lease = leases[0]
        assert (lease.turn_stream_id, lease.operation_id) == ("actual-holder", "actual-create")
        assert (lease.holder_session_id, lease.holder_thread_id) == (SESSION_A, THREAD_A)
        observed.append(lease.lease_id)

    async def create_checked(**arguments: object) -> dict[str, object]:
        record_lease(str(arguments["terminal_id"]))
        return await create(**arguments)

    async def read_checked(rid: str) -> dict[str, object]:
        record_lease(rid)
        return await read(rid)

    async def delete_checked(rid: str) -> dict[str, object]:
        record_lease(rid)
        return await delete(rid)

    monkeypatch.setattr(bundle.transport, "create_terminal", create_checked)
    monkeypatch.setattr(bundle.transport, "read_terminal", read_checked)
    monkeypatch.setattr(bundle.transport, "delete_resource", delete_checked)
    async with bundle.owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A,
        holder_id="actual-holder", operation_id="actual-create",
    ) as operation:
        resource = await operation.create_terminal(retention_scope="thread", title="连续租约")
        rid = str(resource["resource_id"])
        await operation.read_terminal(rid)
        await operation.complete_terminal(rid)
        assert bundle.leases.get_lease(observed[0]).status == "active"
    assert len(observed) == 3
    assert len(set(observed)) == 1
    assert bundle.leases.get_lease(observed[0]).status == "released"


@pytest.mark.asyncio
async def test_management_and_grant_have_real_request_leases_and_do_not_add_associations(
    bundle: Bundle, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    observed: list[tuple[str, str]] = []
    sync = bundle.transport.sync_owner_revision
    grant = bundle.transport.issue_transport_grant

    def record_lease() -> None:
        leases = bundle.leases.leases_for_resource(rid)
        assert len(leases) == 1
        observed.append((leases[0].turn_stream_id, leases[0].operation_id))

    async def sync_checked(terminal_id: str, revision: int) -> None:
        record_lease()
        saved = json.loads((bundle.root / ".boxteam" / "terminal-resources.json").read_text())
        assert saved["records"][0]["owner_revision"] == revision
        await sync(terminal_id, revision)

    async def grant_checked(terminal_id: str, **arguments: object) -> dict[str, object]:
        record_lease()
        return await grant(terminal_id, **arguments)

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", sync_checked)
    monkeypatch.setattr(bundle.transport, "issue_transport_grant", grant_checked)
    await bundle.owner.command(
        rid, action="promote", expected_revision=1,
        holder_id="request:promote", operation_id="req-promote",
    )
    result = await bundle.owner.transport_grant(
        rid, expected_revision=2, holder_id="request:connect", operation_id="req-connect",
    )
    assert observed == [("request:promote", "req-promote"), ("request:connect", "req-connect")]
    assert result["resource"]["thread_associations"] == resource["thread_associations"]
    assert bundle.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
async def test_restart_advances_shadow_revision_revokes_grants_and_preserves_deleted_pruned_receipts(bundle: Bundle) -> None:
    resource = await bundle.create(scope="workspace")
    rid = str(resource["resource_id"])
    grant = await bundle.owner.transport_grant(
        rid, expected_revision=1, holder_id="request:connect", operation_id="connect",
    )
    bundle.transport.records[rid]["transport_owner_revision"] = 10
    bundle.restart()
    recovered = await bundle.owner.get_resource(rid)
    assert recovered["owner_revision"] == 11
    assert grant["grant_token"] not in bundle.transport.grants
    await bundle.command(rid, action="detach", expected_revision=11, thread=ThreadRef(SESSION_A, THREAD_A))
    await bundle.command(rid, action="delete", expected_revision=12)
    del bundle.transport.records[rid]
    bundle.restart()
    assert (await bundle.owner.get_resource(rid))["status"] == "deleted"
    assert await bundle.owner.list_resources() == []


@pytest.mark.asyncio
async def test_stale_release_generation_is_rejected_without_touching_new_runtime_resources(bundle: Bundle) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    bundle.sessions.unloading_generation = 2
    with pytest.raises(ResourceConflict, match="generation"):
        await bundle.owner.release_thread(SESSION_A, THREAD_A, 1)
    assert not bundle.transport.deleted
    assert (await bundle.owner.get_resource(rid))["thread_associations"] == resource["thread_associations"]
    await bundle.owner.release_thread(SESSION_A, THREAD_A, 2)
    assert bundle.transport.deleted == [rid]


@pytest.mark.asyncio
async def test_session_cleanup_checks_entire_lease_set_before_detaching_any_resource(bundle: Bundle) -> None:
    first = await bundle.create(scope="workspace")
    second = await bundle.create(scope="workspace")
    async with bundle.owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A,
        holder_id="actual-holder", operation_id="actual-read",
    ) as operation:
        await operation.get_resource(str(second["resource_id"]))
        with pytest.raises(ResourceConflict, match="未结清"):
            await bundle.owner.cleanup_session(SESSION_A)
        assert (await bundle.owner.get_resource(str(first["resource_id"])))["thread_associations"] == first["thread_associations"]


@pytest.mark.asyncio
async def test_stopped_workspace_terminal_remains_readable_with_operation_lease(bundle: Bundle) -> None:
    resource = await bundle.create(scope="workspace")
    rid = str(resource["resource_id"])
    await bundle.command(rid, action="stop", expected_revision=1)
    async with bundle.owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A, holder_id="holder", operation_id="read-final",
    ) as operation:
        receipt = await operation.read_terminal(rid)
        assert receipt["terminal"]["status"] == "terminated"
        assert len(bundle.leases.leases_for_resource(rid)) == 1
    assert bundle.leases.leases_for_resource(rid) == []


@pytest.mark.asyncio
async def test_unknown_error_preserves_record_without_persisting_absolute_error_path(
    bundle: Bundle, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    await bundle.command(rid, action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))

    async def lose_receipt(terminal_id: str) -> dict[str, object]:
        raise ConnectionError(f"缺失回执: {bundle.root}/private/key")

    monkeypatch.setattr(bundle.transport, "delete_resource", lose_receipt)
    with pytest.raises(ConnectionError):
        await bundle.command(rid, action="delete", expected_revision=2)
    result = (await bundle.owner.list_resources())[0]
    assert result["reconciliation_status"] == "unknown"
    assert str(bundle.root) not in result["error"]
    assert str(bundle.root) not in (bundle.root / ".boxteam" / "terminal-resources.json").read_text()

@pytest.mark.asyncio
async def test_lost_transport_grant_receipt_is_discoverable_unknown(bundle: Bundle, monkeypatch: pytest.MonkeyPatch) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])

    async def lose_receipt(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ConnectionError("授权发送后丢失回执")

    monkeypatch.setattr(bundle.transport, "issue_transport_grant", lose_receipt)
    with pytest.raises(ConnectionError):
        await bundle.owner.transport_grant(
            rid, expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A),
            holder_id="request:lost-grant", operation_id="lost-grant",
        )
    assert bundle.leases.leases_for_resource(rid) == []
    discovered = await bundle.owner.list_resources(thread_id=THREAD_A)
    assert discovered[0]["resource_id"] == rid
    assert discovered[0]["reconciliation_status"] == "unknown"
    assert discovered[0]["available_actions"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["terminated", "exited", "deleted"])
async def test_restart_settles_crashed_operation_only_after_verified_stopped_identity(bundle: Bundle, status: str) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    lease = bundle.leases.acquire_operation(
        resource_id=rid, turn_stream_id="crashed-holder", operation_id="crashed-operation",
        lease_id="crashed-lease", holder_session_id=SESSION_A, holder_thread_id=THREAD_A,
    )
    bundle.transport.records[rid]["status"] = status
    bundle.restart()
    recovered = await bundle.owner.get_resource(rid)
    assert recovered["reconciliation_status"] == "verified"
    assert bundle.leases.get_lease(lease.lease_id).status == "settled"
    assert bundle.leases.leases_for_resource(rid) == []
    assert bundle.transport.creates == 1
    await bundle.owner.cleanup_session(SESSION_A)
    assert (await bundle.owner.get_resource(rid))["status"] == "deleted"


@pytest.mark.asyncio
async def test_running_crashed_operation_stays_pending_until_verified_stopped_readback(bundle: Bundle) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    bundle.leases.acquire_operation(
        resource_id=rid, turn_stream_id="crashed-holder", operation_id="crashed-operation",
        lease_id="crashed-running", holder_session_id=SESSION_A, holder_thread_id=THREAD_A,
    )
    bundle.restart()
    recovered = await bundle.owner.get_resource(rid)
    assert recovered["reconciliation_status"] == "reconcile_required"
    assert bundle.leases.get_lease("crashed-running").status == "reconcile_required"
    with pytest.raises(ResourceConflict):
        await bundle.owner.cleanup_session(SESSION_A)
    bundle.transport.records[rid]["status"] = "terminated"
    recovered = await bundle.owner.get_resource(rid)
    assert recovered["reconciliation_status"] == "reconcile_required"
    await bundle.owner.cleanup_session(SESSION_A)
    assert bundle.leases.get_lease("crashed-running").status == "settled"


@pytest.mark.asyncio
async def test_stopped_unknown_identity_never_settles_crashed_operation(bundle: Bundle) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    bundle.leases.acquire_operation(
        resource_id=rid, turn_stream_id="crashed-holder", operation_id="crashed-operation",
        lease_id="crashed-unknown", holder_session_id=SESSION_A, holder_thread_id=THREAD_A,
    )
    bundle.transport.records[rid].update(status="terminated", reconciliation_status="unknown")
    bundle.restart()
    recovered = await bundle.owner.get_resource(rid)
    assert recovered["reconciliation_status"] == "reconcile_required"
    assert bundle.leases.get_lease("crashed-unknown").status == "reconcile_required"
    with pytest.raises(ResourceConflict):
        await bundle.owner.cleanup_session(SESSION_A)


@pytest.mark.asyncio
async def test_same_generation_cleanup_recovers_lost_delete_receipt_without_replaying(bundle: Bundle, monkeypatch: pytest.MonkeyPatch) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    original = bundle.transport.delete_resource
    async def lose_receipt(resource_id: str) -> dict[str, object]:
        await original(resource_id)
        raise ConnectionError("已删除但回执丢失")
    monkeypatch.setattr(bundle.transport, "delete_resource", lose_receipt)
    with pytest.raises(ConnectionError):
        await bundle.owner.release_thread(SESSION_A, THREAD_A, 1)
    listed = await bundle.owner.list_resources()
    assert len(listed) == 1 and listed[0]["reconciliation_status"] == "unknown"
    assert bundle.transport.deleted == [rid]
    await bundle.owner.release_thread(SESSION_A, THREAD_A, 1)
    assert bundle.transport.deleted == [rid]
    assert await bundle.owner.list_resources() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reconciliation,revision_delta", [("running", "verified", 0), ("deleted", "unknown", 0), ("deleted", "verified", -1)])
async def test_same_generation_cleanup_never_replays_unconfirmed_or_stale_delete(bundle: Bundle, monkeypatch: pytest.MonkeyPatch, status: str, reconciliation: str, revision_delta: int) -> None:
    resource = await bundle.create()
    rid = str(resource["resource_id"])
    calls = []
    async def lose_receipt(resource_id: str) -> dict[str, object]:
        calls.append(resource_id)
        raise ConnectionError("删除结果不可核实")
    monkeypatch.setattr(bundle.transport, "delete_resource", lose_receipt)
    with pytest.raises(ConnectionError):
        await bundle.owner.release_thread(SESSION_A, THREAD_A, 1)
    runtime = bundle.transport.records[rid]
    runtime.update(status=status, reconciliation_status=reconciliation)
    runtime["transport_owner_revision"] += revision_delta
    with pytest.raises(ResourceConflict):
        await bundle.owner.release_thread(SESSION_A, THREAD_A, 1)
    assert calls == [rid]
    assert (await bundle.owner.list_resources())[0]["reconciliation_status"] == "unknown"
