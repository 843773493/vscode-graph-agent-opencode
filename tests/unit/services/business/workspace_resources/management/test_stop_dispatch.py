"""真实回执到达后的本地失败不能重发停止，连续工具仍保留原active占用。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.services.business.workspace_resources.records import ThreadRef
from app.services.infrastructure.external_resource_leases import ResourceLease
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)
from tests.unit.services.business.workspace_resources.test_record_write_lifetime import (
    OwnerFixture,
)


@pytest.mark.parametrize("action", ["stop", "delete"])
@pytest.mark.parametrize("failure_stage", ["receipt", "owner", "ledger", "confirmation"])
@pytest.mark.parametrize("restart", [False, True])
async def test_stop_committed_externally_is_not_replayed_after_local_failure(
    resource_owner: OwnerFixture, monkeypatch: pytest.MonkeyPatch,
    action: str, failure_stage: str, restart: bool,
) -> None:
    bundle = resource_owner
    rid = str((await bundle.create(scope="workspace"))["resource_id"])
    owner, path = bundle.owner, bundle.leases._state_path
    assert owner is not None and path is not None
    revision = 1
    if action == "delete":
        await bundle.command(rid, action="detach", expected_revision=1, thread=ThreadRef(SESSION_A, THREAD_A))
        revision = 2
    stop, delete = bundle.transport.stop_resource, bundle.transport.delete_resource
    publishing, finish = Path.replace, bundle.leases.finish_operation_dispatch
    native, lost = OSError(28, "原停止回执本地提交失败"), ConnectionError("原停止回执丢失")
    observed: list[ResourceLease] = []
    calls: list[str] = []
    committed = False
    failures = 0

    async def stopping(resource_id: str, *, reason: str | None = None) -> dict[str, object]:
        nonlocal committed
        calls.append(resource_id)
        observed.extend(bundle.leases.leases_for_resource(resource_id))
        result = await (delete(resource_id) if action == "delete" else stop(resource_id, reason=reason))
        committed = True
        if failure_stage == "receipt":
            raise lost
        return result

    def unavailable(source: Path, target: Path) -> Path:
        nonlocal failures
        target_path = owner._store.path if failure_stage == "owner" else path if failure_stage == "ledger" else None
        if committed and failures == 0 and target == target_path:
            failures += 1
            raise native
        return publishing(source, target)

    def confirming(lease_id: str) -> None:
        if failure_stage == "confirmation":
            raise native
        finish(lease_id)

    with monkeypatch.context() as fault:
        fault.setattr(bundle.transport, "stop_resource", stopping)
        fault.setattr(bundle.transport, "delete_resource", stopping)
        fault.setattr(Path, "replace", unavailable)
        fault.setattr(bundle.leases, "finish_operation_dispatch", confirming)
        with pytest.raises(ConnectionError if failure_stage == "receipt" else OSError) as caught:
            await bundle.command(rid, action=action, expected_revision=revision)
        if failure_stage == "receipt":
            assert caught.value is lost
        else:
            assert caught.value is native or caught.value.__cause__ is native
    lease, = observed
    assert lease.status == "dispatching" and calls == [rid]
    protected = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert protected is not None and protected.status == ("dispatching" if failure_stage == "confirmation" else "reconcile_required")
    assert bundle.leases.release_turn_leases(lease.turn_stream_id) == []
    current_owner = bundle.restart() if restart else owner
    current = await current_owner.get_resource(rid)
    if not restart:
        assert current["reconciliation_status"] != "verified"
    if current["reconciliation_status"] != "verified":
        current_revision = current["owner_revision"]
        assert type(current_revision) is int
        current = await current_owner.command(
            rid, action=action, expected_revision=current_revision,
            holder_id="request:verify-stopped", operation_id="verify-stopped",
        )
    assert current["reconciliation_status"] == "verified"
    assert current["status"] == ("deleted" if action == "delete" else "terminated" if isinstance(bundle, TerminalOwnerFixture) else "closed")
    settled = type(bundle.leases)(state_path=path).get_lease(lease.lease_id)
    assert settled is not None and settled.status == "settled"
    assert (settled.resource_id, settled.turn_stream_id, settled.operation_id) == (rid, lease.turn_stream_id, lease.operation_id)
    assert calls == [rid] and bundle.transport.deleted == ([rid] if action == "delete" else [])
    assert bundle.transport.creates == 1


@pytest.fixture
def terminal_bundle(tmp_path: Path) -> TerminalOwnerFixture:
    return build_terminal_owner_fixture(tmp_path)


async def test_successful_tool_stop_preserves_same_lease_until_operation_close(
    terminal_bundle: TerminalOwnerFixture,
) -> None:
    bundle = terminal_bundle
    rid = str((await bundle.create(scope="workspace"))["resource_id"])
    owner = bundle.owner
    assert owner is not None
    async with owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A, holder_id="turn:continuous", operation_id="stop-and-read",
    ) as operation:
        await operation.read_terminal(rid)
        lease_id = operation._lease_refs[rid]
        result = await operation.kill_terminal(rid)
        assert result["killed"] is True
        active = bundle.leases.get_lease(lease_id)
        assert active is not None and active.status == "active"
        assert operation._lease_refs[rid] == lease_id
        current = await operation.read_terminal(rid)
        terminal = current["terminal"]
        assert isinstance(terminal, dict) and terminal["status"] == "terminated"
        latest = bundle.leases.get_lease(lease_id)
        assert latest is not None and replace(latest, updated_at=active.updated_at) == active
    released = bundle.leases.get_lease(lease_id)
    assert released is not None and released.status == "released"
