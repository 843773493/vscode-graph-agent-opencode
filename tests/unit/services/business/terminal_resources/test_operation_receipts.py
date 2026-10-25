"""Terminal 全部普通调用及工作区完成查询共用回执核验与占用保护。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from app.abstractions.workspace_resources import ResourceSnapshot
from app.core.resource_uri import parse_resource_uri
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.support.resource_owners.sessions import SESSION_A, THREAD_A
from tests.support.resource_owners.terminal import (
    TerminalOwnerFixture,
    build_terminal_owner_fixture,
)
from tests.unit.services.business.test_terminal_resource_owner import (
    managed_directory as managed_directory,  # noqa: PLC0414
)


@pytest.fixture
def terminal_owner(tmp_path: Path) -> TerminalOwnerFixture:
    return build_terminal_owner_fixture(tmp_path)


@pytest.fixture(params=["get", "read", "write", "background", "claim", "finish", "complete"])
def entry(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def invoke(entry: str) -> Callable[[TerminalOperations, str], Awaitable[ResourceSnapshot]]:
    async def call(operation: TerminalOperations, rid: str) -> ResourceSnapshot:
        if entry == "get":
            return await operation.get_resource(rid)
        if entry == "read":
            return await operation.read_terminal(rid)
        if entry == "write":
            return await operation.write_terminal(terminal_id=rid, data="native-command\n")
        if entry == "background":
            return await operation.mark_terminal_backgrounded(rid)
        if entry == "claim":
            return await operation.claim_terminal_steering(rid, completion_event_id="event", command_start_sequence=1)
        if entry == "finish":
            return await operation.finish_terminal_steering(rid, completion_event_id="event", command_start_sequence=1, dispatched=True)
        return await operation.complete_terminal(rid)
    return call


@pytest.fixture
async def resource(terminal_owner: TerminalOwnerFixture) -> tuple[str, ResourceSnapshot]:
    created = await terminal_owner.create(scope="workspace")
    rid = str(created["resource_id"])
    await terminal_owner.command(rid, action="promote", expected_revision=1)
    return rid, dict(terminal_owner.transport.records[rid])


@pytest.fixture
def reply_method(entry: str) -> str:
    return {
        "get": "get_resource", "read": "read_terminal", "write": "write_terminal",
        "background": "mark_terminal_backgrounded", "claim": "claim_terminal_steering",
        "finish": "finish_terminal_steering", "complete": "get_resource",
    }[entry]


@pytest.mark.parametrize(("field", "value"), [
    ("transport_owner_revision", 1), ("transport_owner_revision", 3),
    ("transport_owner_revision", True), ("transport_owner_revision", "2"),
    ("terminal_id", "term_other"), ("workspace_id", "other-workspace"),
    ("reconciliation_status", "unknown"),
])
async def test_unverified_receipt_cannot_succeed_or_release_the_original_operation(
    terminal_owner: TerminalOwnerFixture, resource: tuple[str, ResourceSnapshot],
    invoke: Callable[[TerminalOperations, str], Awaitable[ResourceSnapshot]], entry: str, reply_method: str,
    monkeypatch: pytest.MonkeyPatch, field: str, value: object,
) -> None:
    rid, runtime = resource
    runtime[field] = value
    seen = []

    async def returned(*args, **kwargs):
        active, = terminal_owner.leases.leases_for_resource(rid)
        seen.append(active)
        return {"terminal": runtime, "output": "retained"} if entry in {"read", "claim", "finish"} else runtime

    monkeypatch.setattr(terminal_owner.transport, reply_method, returned, raising=False)
    async with terminal_owner.owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A, holder_id="receipt-holder", operation_id="receipt-call",
    ) as operation:
        with pytest.raises((ValueError, RuntimeError)):
            await invoke(operation, rid)
    original, = seen
    pending, = terminal_owner.leases.leases_for_resource(rid)
    assert pending.lease_id == original.lease_id
    assert pending.status == "reconcile_required"
    record = terminal_owner.owner._store.get(rid)
    assert record.status == "running" and record.owner_revision == 2
    assert record.reconciliation_status == "unknown"
    cold = ExternalResourceLeaseLedger(state_path=terminal_owner.leases._state_path)
    assert cold.get_lease(original.lease_id) == pending
    terminal_owner.restart()
    current = await terminal_owner.owner.get_resource(rid)
    assert current["reconciliation_status"] == "unknown"
    assert terminal_owner.leases.get_lease(original.lease_id).status == "reconcile_required"
    assert len(seen) == 1


async def test_verified_current_receipt_preserves_envelope_and_normal_operation_release(
    terminal_owner: TerminalOwnerFixture, resource: tuple[str, ResourceSnapshot],
    invoke: Callable[[TerminalOperations, str], Awaitable[ResourceSnapshot]], entry: str, reply_method: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid, runtime = resource

    async def returned(*args, **kwargs):
        return {"terminal": runtime, "output": "retained"} if entry in {"read", "claim", "finish"} else runtime

    monkeypatch.setattr(terminal_owner.transport, reply_method, returned, raising=False)
    async with terminal_owner.owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A, holder_id="current-holder", operation_id="current-call",
    ) as operation:
        result = await invoke(operation, rid)
        projection = result["terminal"] if entry in {"read", "claim", "finish"} else result
        if entry in {"read", "claim", "finish"}:
            assert result["output"] == "retained"
        assert projection["resource_id"] == rid and projection["owner_revision"] == 2
        assert projection["reconciliation_status"] == "verified"
        original, = terminal_owner.leases.leases_for_resource(rid)
        assert original.status == "active"
    assert terminal_owner.leases.get_lease(original.lease_id).status == "released"


async def test_complete_and_other_transport_failures_keep_the_exact_lease(
    terminal_owner: TerminalOwnerFixture, resource: tuple[str, ResourceSnapshot],
    invoke: Callable[[TerminalOperations, str], Awaitable[ResourceSnapshot]], reply_method: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid, _runtime = resource
    error = ConnectionError("receipt missing")
    seen = []

    async def returned(*args, **kwargs):
        original, = terminal_owner.leases.leases_for_resource(rid)
        seen.append(original)
        raise error

    monkeypatch.setattr(terminal_owner.transport, reply_method, returned, raising=False)
    async with terminal_owner.owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A, holder_id="missing-holder", operation_id="missing-call",
    ) as operation:
        with pytest.raises(ConnectionError) as caught:
            await invoke(operation, rid)
        assert caught.value is error
    original, = seen
    assert terminal_owner.leases.get_lease(original.lease_id).status == "reconcile_required"
    assert terminal_owner.owner._store.get(rid).reconciliation_status == "unknown"


async def test_workspace_completion_commits_verified_terminal_state_and_settles_only_directory_use(
    terminal_owner: TerminalOwnerFixture, managed_directory: str,
) -> None:
    created = await terminal_owner.create(scope="workspace")
    rid = str(created["resource_id"])
    attachment = parse_resource_uri(managed_directory).tail_segments[0]
    directory_lease, = terminal_owner.leases.leases_for_resource(attachment)
    terminal_owner.transport.records[rid]["status"] = "terminated"
    async with terminal_owner.owner.operation(
        session_id=SESSION_A, thread_id=THREAD_A, holder_id="complete-holder", operation_id="complete-call",
    ) as operation:
        result = await operation.complete_terminal(rid)
        assert result["status"] == "terminated"
        assert terminal_owner.owner._store.get(rid).status == "terminated"
        assert terminal_owner.leases.get_lease(directory_lease.lease_id).status == "settled"
        active, = terminal_owner.leases.leases_for_resource(rid)
        assert active.status == "active"
        assert (active.turn_stream_id, active.operation_id) == ("complete-holder", "complete-call")
        assert result["retention_scope"] == "workspace" and result["thread_associations"]
    assert terminal_owner.leases.get_lease(active.lease_id).status == "released"
    assert not terminal_owner.transport.deleted
