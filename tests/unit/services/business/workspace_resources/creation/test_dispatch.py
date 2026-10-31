"""创建回执丢失且失败写盘不可用时，普通收尾仍保留原占用。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.infrastructure.external_resource_leases import ResourceLease
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.unit.services.business.workspace_resources.creation.conftest import (
    Operation,
    create,
)
from tests.unit.services.business.workspace_resources.test_record_write_lifetime import (
    OwnerFixture,
)


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("storage", ["ledger", "owner"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_lost_creation_receipt_keeps_exact_lease_after_failed_unknown_and_cleanup(
    resource_owner: OwnerFixture, operation: Operation,
    monkeypatch: pytest.MonkeyPatch, restart: bool, storage: str, cancel: bool,
) -> None:
    owner, ledger_path = resource_owner.owner, resource_owner.leases._state_path
    assert owner is not None and ledger_path is not None
    method = "create_terminal" if isinstance(operation, TerminalOperations) else "create_browser"
    original_create = getattr(resource_owner.transport, method)
    original = asyncio.CancelledError("创建回执原取消") if cancel else ConnectionError("创建回执丢失")
    native = OSError(28, "回执丢失后的存储不可用")
    replace = Path.replace
    observed: list[ResourceLease] = []
    dispatched = False

    async def lost(**arguments: object):
        nonlocal dispatched
        rid = str(arguments[f"{owner.kind}_id"])
        lease, = type(resource_owner.leases)(state_path=ledger_path).leases_for_resource(rid)
        assert (lease.resource_id, lease.turn_stream_id, lease.operation_id,
                lease.holder_session_id, lease.holder_thread_id) == (
            rid, "turn:creation", "create:original", SESSION_A, THREAD_A,
        )
        observed.append(lease)
        await original_create(**arguments)
        dispatched = True
        raise original

    def unavailable(source: Path, target: Path) -> Path:
        if dispatched and target == (ledger_path if storage == "ledger" else owner._store.path):
            raise native
        return replace(source, target)

    with monkeypatch.context() as fault:
        fault.setattr(resource_owner.transport, method, lost)
        fault.setattr(Path, "replace", unavailable)
        with pytest.raises(OSError) as caught:
            await create(operation)
        assert caught.value.__cause__ is native and caught.value.__context__ is original
    lease, = observed
    rid = lease.resource_id
    protected = type(resource_owner.leases)(state_path=ledger_path).get_lease(lease.lease_id)
    assert protected is not None
    await operation.close()
    assert resource_owner.leases.release_turn_leases(lease.turn_stream_id) == []
    assert type(resource_owner.leases)(state_path=ledger_path).get_lease(lease.lease_id) == protected
    assert protected.status == ("dispatching" if storage == "ledger" else "reconcile_required")
    assert resource_owner.transport.records[rid]["status"] == "running"
    recovered_owner = resource_owner.restart() if restart else owner
    current = await recovered_owner.get_resource(rid)
    assert current["reconciliation_status"] != "verified" and current["available_actions"] == ["stop"]
    retained = resource_owner.leases.get_lease(lease.lease_id)
    assert retained is not None
    assert (retained.lease_id, retained.resource_id, retained.operation_id, retained.turn_stream_id,
            retained.holder_session_id, retained.holder_thread_id) == (
        lease.lease_id, rid, "create:original", "turn:creation", SESSION_A, THREAD_A,
    )
    external = resource_owner.leases.get(rid)
    assert external is not None and external.created_by_turn_id == "origin-turn"
    stopped = await resource_owner.command(rid, action="stop", expected_revision=current["owner_revision"])
    assert stopped["status"] == ("terminated" if isinstance(operation, TerminalOperations) else "closed")
    assert stopped["reconciliation_status"] == "verified"
    settled = type(resource_owner.leases)(state_path=ledger_path).get_lease(lease.lease_id)
    assert settled is not None and settled.status == "settled" and settled.operation_id == lease.operation_id
    assert resource_owner.transport.creates == 1 and resource_owner.transport.deleted == []


async def test_successful_creation_keeps_same_original_lease_until_operation_close(
    resource_owner: OwnerFixture, operation: Operation, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, path = resource_owner.owner, resource_owner.leases._state_path
    assert owner is not None and path is not None
    method = "create_terminal" if isinstance(operation, TerminalOperations) else "create_browser"
    original = getattr(resource_owner.transport, method)
    observed: list[ResourceLease] = []

    async def checked(**arguments: object):
        rid = str(arguments[f"{owner.kind}_id"])
        lease, = type(resource_owner.leases)(state_path=path).leases_for_resource(rid)
        assert lease.status == "dispatching"
        assert resource_owner.leases.release_turn_leases(lease.turn_stream_id) == []
        observed.append(lease)
        return await original(**arguments)

    monkeypatch.setattr(resource_owner.transport, method, checked)
    result = await create(operation)
    lease, = observed
    confirmed = type(resource_owner.leases)(state_path=path).get_lease(lease.lease_id)
    assert confirmed is not None and confirmed.status == "active" and confirmed.operation_id == lease.operation_id
    record = owner._store.get(lease.resource_id)
    assert record is not None and record.reconciliation_status == "verified"
    assert result["resource_id"] == lease.resource_id and result["owner_revision"] == record.owner_revision == 1
    await operation.close()
    released = type(resource_owner.leases)(state_path=path).get_lease(lease.lease_id)
    assert released is not None and released.status == "released" and resource_owner.transport.creates == 1
