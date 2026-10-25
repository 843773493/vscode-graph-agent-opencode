"""Browser 两类操作入口只接受本次版本的完整回执，校验失败保留原占用。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.abstractions.workspace_resources import ResourceSnapshot
from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
    ResourceLease,
)
from tests.support.resource_owners.browser import (
    BrowserOwnerFixture,
    build_browser_owner_fixture,
)
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A


@pytest.fixture
def browser_owner(tmp_path: Path) -> BrowserOwnerFixture:
    return build_browser_owner_fixture(tmp_path)


@pytest.fixture(params=["tool", "management"])
def invoke(
    browser_owner: BrowserOwnerFixture, request: pytest.FixtureRequest,
) -> Callable[[str], Awaitable[ResourceSnapshot]]:
    async def call(rid: str) -> ResourceSnapshot:
        owner = browser_owner.owner
        assert owner is not None
        if request.param == "management":
            return await owner.perform(
                rid, action="read", payload={}, expected_revision=2,
                holder_id="receipt-holder", operation_id="receipt-operation",
                thread=ThreadRef(SESSION_A, THREAD_A),
            )
        async with owner.operation(
            session_id=SESSION_A, thread_id=THREAD_A,
            holder_id="receipt-holder", operation_id="receipt-operation",
        ) as operation:
            return await operation.perform(rid, "read")
    return call


@pytest.fixture(params=[False, True], ids=["success", "failed"])
def receipt(
    browser_owner: BrowserOwnerFixture, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> tuple[ResourceSnapshot, BrowserRequestError | None, list[ResourceLease]]:
    payload: ResourceSnapshot = {"summary": "receipt", "operation_outcome": "failed"}
    failure = BrowserRequestError(method="POST", path="/read", status=408, payload=payload) if request.param else None
    acquired: list[ResourceLease] = []

    async def returned(rid: str, action: str, values: ResourceSnapshot, *, actor: str) -> ResourceSnapshot:
        lease, = browser_owner.leases.leases_for_resource(rid)
        assert lease.status == "active"
        acquired.append(lease)
        if failure is not None:
            raise failure
        return payload

    monkeypatch.setattr(browser_owner.transport, "perform", returned)
    return payload, failure, acquired


async def prepared(browser_owner: BrowserOwnerFixture) -> tuple[str, ResourceSnapshot]:
    browser = await browser_owner.create()
    rid = str(browser["resource_id"])
    await browser_owner.command(rid, action="promote", expected_revision=1)
    return rid, dict(browser_owner.transport.records[rid])


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [
    ("browser_id", "browser_other"), ("workspace_id", "other-workspace"),
    ("transport_owner_revision", 1), ("transport_owner_revision", 3),
    ("transport_owner_revision", True), ("transport_owner_revision", "2"),
    ("reconciliation_status", "unknown"), ("cwd", "/native/private"),
    ("status", None), ("title", None),
])
async def test_invalid_receipt_preserves_the_original_lease_after_scope_and_restart(
    browser_owner: BrowserOwnerFixture, invoke: Callable[[str], Awaitable[ResourceSnapshot]],
    receipt: tuple[ResourceSnapshot, BrowserRequestError | None, list[ResourceLease]],
    field: str, value: object,
) -> None:
    rid, runtime = await prepared(browser_owner)
    payload, failure, acquired = receipt
    if value is None:
        runtime.pop(field)
    else:
        runtime[field] = value
    payload["browser"] = runtime
    with pytest.raises((ValueError, RuntimeError, KeyError, TypeError)) as caught:
        await invoke(rid)
    assert browser_owner.owner is not None
    record = browser_owner.owner._store.get(rid)
    assert record.reconciliation_status == "unknown"
    assert record.owner_revision == 2
    assert record.status == "running"
    original, = acquired
    pending, = browser_owner.leases.leases_for_resource(rid)
    assert pending.lease_id == original.lease_id
    assert pending.status == "reconcile_required"
    assert (pending.turn_stream_id, pending.operation_id) == ("receipt-holder", "receipt-operation")
    if failure is not None and field == "reconciliation_status":
        assert caught.value is failure
    elif failure is not None:
        assert caught.value.__cause__ is failure
    cold = ExternalResourceLeaseLedger(state_path=browser_owner.leases._state_path)
    assert cold.get_lease(original.lease_id) == pending
    owner = browser_owner.restart()
    current = await owner.get_resource(rid)
    assert current["reconciliation_status"] == "unknown"
    assert browser_owner.leases.get_lease(original.lease_id).status == "reconcile_required"
    assert len(acquired) == 1


@pytest.mark.asyncio
async def test_valid_receipt_preserves_the_original_result_or_failure_and_releases_its_lease(
    browser_owner: BrowserOwnerFixture, invoke: Callable[[str], Awaitable[ResourceSnapshot]],
    receipt: tuple[ResourceSnapshot, BrowserRequestError | None, list[ResourceLease]],
) -> None:
    rid, runtime = await prepared(browser_owner)
    payload, failure, acquired = receipt
    payload["browser"] = runtime
    if failure is not None:
        with pytest.raises(BrowserRequestError) as caught:
            await invoke(rid)
        assert caught.value is failure
    else:
        result = await invoke(rid)
        assert result["summary"] == "receipt"
        assert result["browser"]["owner_revision"] == 2
    original, = acquired
    assert browser_owner.leases.get_lease(original.lease_id).status == "released"
    assert browser_owner.leases.leases_for_resource(rid) == []
    assert browser_owner.owner._store.get(rid).reconciliation_status == "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["record", "ledger"])
async def test_receipt_commit_failure_keeps_the_original_operation_for_reconciliation(
    browser_owner: BrowserOwnerFixture, invoke: Callable[[str], Awaitable[ResourceSnapshot]],
    receipt: tuple[ResourceSnapshot, BrowserRequestError | None, list[ResourceLease]],
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    rid, runtime = await prepared(browser_owner)
    payload, failure, acquired = receipt
    payload["browser"] = runtime
    owner = browser_owner.owner
    assert owner is not None
    target = owner._store if stage == "record" else owner
    method = "save" if stage == "record" else "_sync_ledger"
    original = getattr(target, method)
    error = OSError("receipt commit failed")
    attempts = 0

    def rejected(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(target, method, rejected)
    with pytest.raises(OSError) as caught:
        await invoke(rid)
    assert caught.value is error
    if failure is not None:
        assert caught.value.__cause__ is failure
    lease, = acquired
    assert owner._store.get(rid).reconciliation_status == "unknown"
    assert browser_owner.leases.get_lease(lease.lease_id).status == "reconcile_required"
    cold = ExternalResourceLeaseLedger(state_path=browser_owner.leases._state_path)
    assert cold.get_lease(lease.lease_id) == browser_owner.leases.get_lease(lease.lease_id)
