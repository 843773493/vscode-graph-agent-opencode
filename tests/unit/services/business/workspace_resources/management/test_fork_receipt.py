"""提交后的取消只消费原fork证明，失败及损坏身份不能解除派发保护。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.business.workspace_resources import owner as owner_module
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


@pytest.mark.parametrize("failure_stage", ["none", "lease"])
async def test_committed_fork_cancellation_keeps_original_receipt_through_confirmation_failure(
    resource_owner: OwnerFixture, monkeypatch: pytest.MonkeyPatch, failure_stage: str,
) -> None:
    bundle = resource_owner
    rid = str((await bundle.create(scope="workspace"))["resource_id"])
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    target, key = ThreadRef(SESSION_B, THREAD_B), "fork-management-cancel"
    original = asyncio.CancelledError("fork提交后原取消")
    native = OSError(28, "fork提交后原账本确认失败")
    observe, publishing = owner_module.observe_phase, Path.replace
    observed: list[ResourceLease] = []
    committed = False
    writes = 0

    async def observing(operation_id: str, phase: str, *, details: dict[str, object] | None = None) -> None:
        nonlocal committed
        if operation_id == key and phase == f"fork.live.committed.{owner.kind}":
            observed.extend(bundle.leases.leases_for_resource(rid))
            committed = True
            raise original
        await observe(operation_id, phase, details=details)

    def unavailable(source: Path, destination: Path) -> Path:
        nonlocal writes
        if committed and destination == path:
            writes += 1
            if failure_stage == "lease":
                raise native
        return publishing(source, destination)

    with monkeypatch.context() as fault:
        fault.setattr(owner_module, "observe_phase", observing)
        fault.setattr(Path, "replace", unavailable)
        with pytest.raises(asyncio.CancelledError) as caught:
            await owner.attach_for_fork(rid, creation_key=key, expected_revision=1, thread=target)
        assert caught.value is original
        if failure_stage != "none":
            assert caught.value.__cause__ is not None and caught.value.__cause__.__cause__ is native
    assert writes == 1, "fork回执确认直接原子释放，不经active中间写入"
    lease, = observed
    assert lease.status == "dispatching"
    frozen = owner.fork_attach_receipt(rid, creation_key=key, expected_revision=1, thread=target)
    assert frozen is not None and frozen.state == "committed" and frozen.committed_revision == 2
    assert frozen.lease_id == lease.lease_id
    saved = owner._store.get(rid)
    assert saved is not None and saved.thread_associations.count(target) == 1
    assert saved.owner_revision == 2 and saved.reconciliation_status == "verified"
    retained = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert retained is not None and retained.status == ("released" if failure_stage == "none" else "dispatching")
    assert bundle.leases.release_turn_leases(lease.turn_stream_id) == []
    # 冷恢复消费原证明；同key不再次关联或篡改历史提交版本。
    cold = bundle.restart()
    current, receipt = await cold.attach_for_fork(rid, creation_key=key, expected_revision=1, thread=target)
    assert receipt == frozen and current["reconciliation_status"] == "verified"
    assert current["owner_revision"] == 3 and bundle.transport.creates == 1
    saved = cold._store.get(rid)
    assert saved is not None and saved.thread_associations.count(target) == 1
    released = bundle.leases.get_lease(lease.lease_id)
    assert released is not None and released.status == "released"


@pytest.mark.parametrize("field", ["resource_id", "turn_stream_id", "operation_id", "holder_session_id", "holder_thread_id"])
async def test_committed_fork_checks_all_lease_identity_before_restoring_active(
    resource_owner: OwnerFixture, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    bundle = resource_owner
    rid = str((await bundle.create(scope="workspace"))["resource_id"])
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    target, key = ThreadRef(SESSION_B, THREAD_B), "fork-damaged-identity"
    original = asyncio.CancelledError("fork身份待核实原取消")
    observe, get_lease = owner_module.observe_phase, bundle.leases.get_lease
    observed: list[ResourceLease] = []

    async def observing(operation_id: str, phase: str, *, details: dict[str, object] | None = None) -> None:
        if operation_id == key and phase == f"fork.live.committed.{owner.kind}":
            observed.extend(bundle.leases.leases_for_resource(rid))
            raise original
        await observe(operation_id, phase, details=details)

    def damaged(lease_id: str) -> ResourceLease | None:
        lease = get_lease(lease_id)
        if observed and lease_id == observed[0].lease_id and lease is not None:
            value = SESSION_A if field == "holder_session_id" else THREAD_A if field == "holder_thread_id" else "damaged"
            return replace(lease, **{field: value})
        return lease

    with monkeypatch.context() as fault:
        fault.setattr(owner_module, "observe_phase", observing)
        fault.setattr(bundle.leases, "get_lease", damaged)
        with pytest.raises(asyncio.CancelledError) as caught:
            await owner.attach_for_fork(rid, creation_key=key, expected_revision=1, thread=target)
        assert caught.value is original
        assert isinstance(caught.value.__cause__, RuntimeError)
        assert "回执与原 operation lease 身份冲突" in str(caught.value.__cause__)
    lease, = observed
    retained = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert retained is not None and retained == lease and retained.status == "dispatching"
    assert bundle.leases.release_turn_leases(lease.turn_stream_id) == []
    cold = bundle.restart()
    _, receipt = await cold.attach_for_fork(rid, creation_key=key, expected_revision=1, thread=target)
    assert receipt.state == "committed" and receipt.lease_id == lease.lease_id
    released = bundle.leases.get_lease(lease.lease_id)
    assert released is not None and released.status == "released"
