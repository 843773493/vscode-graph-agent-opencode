"""版本同步回执和unknown写盘同时失败时，管理收尾仍保护原占用。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.external_resource_leases import ResourceLease
from tests.support.resource_owners.sessions import (
    SESSION_A,
    SESSION_B,
    THREAD_A,
    THREAD_B,
)
from tests.unit.services.business.workspace_resources.test_record_write_lifetime import (
    OwnerFixture,
)


@pytest.mark.parametrize("action", ["promote", "attach", "detach", "release-thread", "release-session", "stop", "delete"])
@pytest.mark.parametrize("storage", ["ledger", "owner"])
@pytest.mark.parametrize("restart", [False, True])
async def test_unknown_write_failure_protects_exact_management_lease(
    resource_owner: OwnerFixture, monkeypatch: pytest.MonkeyPatch,
    action: str, storage: str, restart: bool,
) -> None:
    bundle = resource_owner
    rid = str((await bundle.create(scope="thread" if action == "promote" else "workspace"))["resource_id"])
    owner, ledger_path = bundle.owner, bundle.leases._state_path
    assert owner is not None and ledger_path is not None
    revision = 1
    if action == "delete":
        await bundle.command(rid, action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
        revision = 2
    original_record = owner._store.get(rid)
    sync, replace = bundle.transport.sync_owner_revision, Path.replace
    failure, native = ConnectionError("原管理版本回执丢失"), OSError(28, "unknown原写盘失败")
    observed: list[ResourceLease] = []
    dispatched = False
    failures = 0

    async def lost(resource_id: str, owner_revision: int, *, for_stop: bool) -> None:
        nonlocal dispatched
        observed.extend(bundle.leases.leases_for_resource(resource_id))
        await sync(resource_id, owner_revision, for_stop=for_stop)
        dispatched = True
        raise failure

    def unavailable(source: Path, target: Path) -> Path:
        nonlocal failures
        if dispatched and target == (ledger_path if storage == "ledger" else owner._store.path):
            failures += 1
            raise native
        return replace(source, target)

    with monkeypatch.context() as fault:
        fault.setattr(bundle.transport, "sync_owner_revision", lost)
        fault.setattr(Path, "replace", unavailable)
        with pytest.raises(OSError) as caught:
            if action == "release-thread":
                await owner.release_thread(SESSION_A, THREAD_A, 1)
            elif action == "release-session":
                await owner.cleanup_session(SESSION_A)
            else:
                await bundle.command(
                    rid, action=action, expected_revision=revision,
                    thread=ThreadRef(SESSION_B, THREAD_B) if action == "attach" else ThreadRef(SESSION_A, THREAD_A),
                )
        assert caught.value.__cause__ is native and caught.value.__context__ is failure
        assert failures == 1, "管理finally不得再次写盘误释放或覆盖原失败"
    lease, = observed
    assert lease.status == "dispatching" and lease.resource_id == rid
    assert bundle.leases.release_turn_leases(lease.turn_stream_id) == []
    protected = type(bundle.leases)(state_path=ledger_path).get_lease(lease.lease_id)
    assert protected is not None and protected.status == ("dispatching" if storage == "ledger" else "reconcile_required")
    saved = owner._store.get(rid)
    assert saved is not None and original_record is not None
    assert saved.owner_revision == revision + 1
    assert saved.thread_associations == original_record.thread_associations
    assert saved.retention_scope == original_record.retention_scope
    current_owner = bundle.restart() if restart else owner
    current = await current_owner.get_resource(rid)
    actions = current["available_actions"]
    assert isinstance(actions, list) and "stop" in actions
    assert current["reconciliation_status"] != "verified"
    retained = bundle.leases.get_lease(lease.lease_id)
    assert retained is not None and retained.status in {"dispatching", "reconcile_required"}
    assert (retained.lease_id, retained.resource_id, retained.turn_stream_id, retained.operation_id,
            retained.holder_session_id, retained.holder_thread_id) == (
        lease.lease_id, rid, lease.turn_stream_id, lease.operation_id, lease.holder_session_id, lease.holder_thread_id,
    )
    # 删除准备不改写为stop；新的管理请求沿原版本核实，不能重放旧关联命令。
    current_revision = current["owner_revision"]
    assert type(current_revision) is int
    result = await current_owner.command(
        rid, action="delete" if action == "delete" else "stop", expected_revision=current_revision,
        holder_id="request:verify-management", operation_id="verify-management",
    )
    assert result["reconciliation_status"] == "verified"
    settled = type(bundle.leases)(state_path=ledger_path).get_lease(lease.lease_id)
    assert settled is not None and settled.status == "settled"
    assert bundle.transport.creates == 1


@pytest.mark.parametrize("action", ["promote", "attach", "detach"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_successful_management_confirmation_and_original_cancellation(
    resource_owner: OwnerFixture, monkeypatch: pytest.MonkeyPatch, action: str, cancel: bool,
) -> None:
    bundle = resource_owner
    rid = str((await bundle.create(scope="thread" if action == "promote" else "workspace"))["resource_id"])
    owner = bundle.owner
    assert owner is not None
    sync = bundle.transport.sync_owner_revision
    original = asyncio.CancelledError("原管理同步取消")
    observed: list[ResourceLease] = []

    async def checked(resource_id: str, revision: int, *, for_stop: bool) -> None:
        lease, = bundle.leases.leases_for_resource(resource_id)
        assert lease.status == "dispatching"
        observed.append(lease)
        await sync(resource_id, revision, for_stop=for_stop)
        if cancel:
            raise original

    monkeypatch.setattr(bundle.transport, "sync_owner_revision", checked)
    target = ThreadRef(SESSION_B, THREAD_B) if action == "attach" else ThreadRef(SESSION_A, THREAD_A)
    if cancel:
        with pytest.raises(asyncio.CancelledError) as caught:
            await bundle.command(rid, action=action, expected_revision=1, thread=target)
        assert caught.value is original
    else:
        result = await bundle.command(rid, action=action, expected_revision=1, thread=target)
        assert result["owner_revision"] == 2 and result["reconciliation_status"] == "verified"
        assert result["retention_scope"] == "workspace"
        saved = owner._store.get(rid)
        assert saved is not None and (target in saved.thread_associations) == (action != "detach")
    lease, = observed
    final = bundle.leases.get_lease(lease.lease_id)
    assert final is not None and final.status == ("reconcile_required" if cancel else "released")
