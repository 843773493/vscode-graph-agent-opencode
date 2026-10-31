"""已核实的未签发冲突正常释放原占用；无效拒绝回执不能冒充核实。"""

from __future__ import annotations

import pytest

from app.abstractions.workspace_resources import ResourceTransportGrantRejected
from app.services.business.workspace_resources.owner import ResourceConflict
from app.services.infrastructure.external_resource_leases import ResourceLease
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.workspace_resources.test_transport_grant_deadline import (
    bundle as bundle,  # noqa: PLC0414
)
from tests.unit.services.business.workspace_resources.test_transport_grant_deadline import (
    grant as grant,  # noqa: PLC0414
)


@pytest.mark.parametrize("outcome", ["stopped", "new-version"])
async def test_preflight_snapshot_conflict_does_not_issue_or_leave_unknown_lease(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, grant,
    monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    observed: list[ResourceLease] = []
    issued: list[str] = []

    async def snapshot(resource_id: str):
        observed.extend(bundle.leases.leases_for_resource(resource_id))
        result = dict(bundle.transport.records[resource_id])
        if outcome == "new-version":
            result["transport_owner_revision"] = 2
        else:
            result["status"] = "terminated" if isinstance(bundle, TerminalOwnerFixture) else "closed"
        bundle.transport.records[resource_id] = result
        return result

    async def unexpected(resource_id: str, **_arguments):
        issued.append(resource_id)
        raise AssertionError("预检冲突不得签发授权")

    monkeypatch.setattr(bundle.transport, "get_resource", snapshot)
    monkeypatch.setattr(bundle.transport, "issue_transport_grant", unexpected)
    with pytest.raises(ResourceConflict) as caught:
        await grant(rid)
    assert str(caught.value) == "外部资源 运行状态或传输授权版本已改变，请刷新资源"
    assert caught.value.__cause__ is None and issued == []
    lease, = observed
    released = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert released is not None and released.status == "released" and released.operation_id == lease.operation_id
    record = owner._store.get(rid)
    assert record is not None and record.reconciliation_status == "verified"
    assert record.owner_revision == (2 if outcome == "new-version" else 1)
    assert record.status == bundle.transport.records[rid]["status"]


@pytest.mark.parametrize("outcome", ["stopped", "new-version"])
async def test_verified_grant_rejection_keeps_exact_conflict_and_current_snapshot(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, grant,
    monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    observed: list[ResourceLease] = []
    rejection: ResourceTransportGrantRejected | None = None

    async def reject(resource_id: str, **_arguments):
        nonlocal rejection
        observed.extend(bundle.leases.leases_for_resource(resource_id))
        snapshot = dict(bundle.transport.records[resource_id])
        if outcome == "new-version":
            snapshot["transport_owner_revision"] = 2
        else:
            snapshot["status"] = "terminated" if isinstance(bundle, TerminalOwnerFixture) else "closed"
        bundle.transport.records[resource_id] = snapshot
        rejection = ResourceTransportGrantRejected("原授权拒绝", snapshot=snapshot)
        raise rejection

    monkeypatch.setattr(bundle.transport, "issue_transport_grant", reject)
    with pytest.raises(ResourceConflict) as caught:
        await grant(rid)
    assert str(caught.value) == "外部资源 运行状态或传输授权版本已改变，请刷新资源"
    assert caught.value.__cause__ is rejection and rejection is not None
    lease, = observed
    record = owner._store.get(rid)
    assert record is not None and record.reconciliation_status == "verified"
    assert record.status == rejection.snapshot["status"]
    assert record.owner_revision == rejection.snapshot["transport_owner_revision"]
    released = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert released is not None and released.status == "released" and released.operation_id == lease.operation_id
    assert bundle.transport.creates == 1 and bundle.transport.deleted == []


@pytest.mark.parametrize("invalid", ["same-version", "wrong-workspace", "wrong-resource", "unverified", "boolean-version", "old-version"])
async def test_invalid_grant_rejection_keeps_original_unknown_lease(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, grant,
    monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    snapshot = dict(bundle.transport.records[rid])
    snapshot["transport_owner_revision"] = 2
    if invalid == "same-version":
        snapshot["transport_owner_revision"] = 1
    elif invalid == "wrong-workspace":
        snapshot["workspace_id"] = "other-workspace"
    elif invalid == "wrong-resource":
        snapshot[f"{owner.kind}_id"] = "other-resource"
    elif invalid == "unverified":
        snapshot["reconciliation_status"] = "unknown"
    elif invalid == "boolean-version":
        snapshot["transport_owner_revision"] = True
    else:
        snapshot["transport_owner_revision"] = 0
    failure = ResourceTransportGrantRejected("原拒绝含无效回执", snapshot=snapshot)
    observed: list[ResourceLease] = []

    async def reject(resource_id: str, **_arguments):
        observed.extend(bundle.leases.leases_for_resource(resource_id))
        raise failure

    monkeypatch.setattr(bundle.transport, "issue_transport_grant", reject)
    with pytest.raises(ValueError) as caught:
        await grant(rid)
    assert caught.value.__context__ is failure
    lease, = observed
    protected = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert protected is not None and protected.status == "reconcile_required" and protected.operation_id == lease.operation_id
    record = owner._store.get(rid)
    assert record is not None and record.owner_revision == 1 and record.reconciliation_status == "unknown"
    assert record.status == "running" and bundle.transport.creates == 1
