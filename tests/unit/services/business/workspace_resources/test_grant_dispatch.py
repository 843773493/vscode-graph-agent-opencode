"""签发回执丢失及失败写盘不可用时，管理和Turn收尾保留同一原占用。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.infrastructure.external_resource_leases import ResourceLease
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import TerminalOwnerFixture
from tests.unit.services.business.workspace_resources.test_transport_grant_deadline import (
    bundle as bundle,  # noqa: PLC0414
)
from tests.unit.services.business.workspace_resources.test_transport_grant_deadline import (
    grant as grant,  # noqa: PLC0414
)


@pytest.mark.parametrize("storage", ["ledger", "owner"])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("persistent", [False, True], ids=["once", "unavailable"])
async def test_failed_unknown_persistence_cannot_release_an_unconfirmed_grant(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, grant,
    monkeypatch: pytest.MonkeyPatch, storage: str, restart: bool, cancel: bool, persistent: bool,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    issue, replace = bundle.transport.issue_transport_grant, Path.replace
    failure = asyncio.CancelledError("原签发回执取消") if cancel else ConnectionError("原签发回执丢失")
    native = OSError(28, "签发后原存储写入失败")
    observed: list[ResourceLease] = []
    issued: list[dict[str, object]] = []
    failures = 0

    async def lost(resource_id: str, **arguments):
        observed.extend(bundle.leases.leases_for_resource(resource_id))
        issued.append(await issue(resource_id, **arguments))
        raise failure

    def unavailable(source: Path, target: Path) -> Path:
        nonlocal failures
        if issued and target == (path if storage == "ledger" else owner._store.path) and (persistent or failures == 0):
            failures += 1
            raise native
        return replace(source, target)

    with monkeypatch.context() as fault:
        fault.setattr(bundle.transport, "issue_transport_grant", lost)
        fault.setattr(Path, "replace", unavailable)
        with pytest.raises(OSError) as caught:
            await grant(rid)
        assert caught.value.__cause__ is native and caught.value.__context__ is failure
        assert failures == 1, "管理finally不得尝试释放未核实授权并用次错覆盖原失败"
    lease, = observed
    assert (lease.resource_id, lease.turn_stream_id, lease.operation_id, lease.holder_session_id,
            lease.holder_thread_id) == (rid, "request:grant-under-test", "grant-under-test", SESSION_A, THREAD_A)
    assert len(issued) == 1 and issued[0]["resource_id"] == rid and issued[0]["owner_revision"] == 1
    assert issued[0]["grant_token"] and bundle.transport.records[rid]["status"] == "running"
    # 原管理context已收尾；随后普通Turn收尾也不能丢掉未核实签发。
    assert bundle.leases.release_turn_leases(lease.turn_stream_id) == []
    protected = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert protected is not None and protected.status == ("dispatching" if storage == "ledger" else "reconcile_required")
    recovered_owner = bundle.restart() if restart else owner
    current = await recovered_owner.get_resource(rid)
    assert current["reconciliation_status"] != "verified" and current["available_actions"] == ["stop"]
    retained = bundle.leases.get_lease(lease.lease_id)
    assert retained is not None and retained.status in {"dispatching", "reconcile_required"}
    assert (retained.lease_id, retained.resource_id, retained.turn_stream_id, retained.operation_id,
            retained.holder_session_id, retained.holder_thread_id) == (
        lease.lease_id, rid, lease.turn_stream_id, lease.operation_id, SESSION_A, THREAD_A,
    )
    stopped = await bundle.command(rid, action="stop", expected_revision=current["owner_revision"])
    assert stopped["reconciliation_status"] == "verified"
    assert stopped["status"] == ("terminated" if isinstance(bundle, TerminalOwnerFixture) else "closed")
    settled = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert settled is not None and settled.status == "settled"
    assert settled.operation_id == lease.operation_id and len(issued) == 1


async def test_grant_is_protected_before_emission_and_released_only_after_confirmation(
    bundle: TerminalOwnerFixture | BrowserOwnerFixture, grant, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = str((await bundle.create())["resource_id"])
    path = bundle.leases._state_path
    assert path is not None
    issue = bundle.transport.issue_transport_grant
    observed: list[ResourceLease] = []

    async def checked(resource_id: str, **arguments):
        lease, = type(bundle.leases)(state_path=path).leases_for_resource(resource_id)
        assert lease.status == "dispatching"
        assert bundle.leases.release_turn_leases(lease.turn_stream_id) == []
        observed.append(lease)
        return await issue(resource_id, **arguments)

    monkeypatch.setattr(bundle.transport, "issue_transport_grant", checked)
    result = await grant(rid)
    lease, = observed
    assert result["resource"]["resource_id"] == rid and result["resource"]["reconciliation_status"] == "verified"
    released = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert released is not None and released.status == "released" and released.operation_id == lease.operation_id
