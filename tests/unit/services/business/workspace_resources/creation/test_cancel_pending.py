"""旧创建意图只能由新管理operation消费原生取消证明，不能按404结清。"""

from __future__ import annotations

import pytest

from app.services.business.workspace_resources.owner import ResourceConflict
from tests.support.resource_owners.sessions import SESSION_A
from tests.unit.services.business.workspace_resources.creation.conftest import (
    Operation,
    create,
)
from tests.unit.services.business.workspace_resources.test_record_write_lifetime import (
    OwnerFixture,
)


@pytest.mark.parametrize("bad_receipt", ["none", "revision", "boolean", "workspace", "outcome", "resource"])
async def test_pending_creation_requires_exact_durable_receipt_and_new_management_identity(
    resource_owner: OwnerFixture, operation: Operation, monkeypatch: pytest.MonkeyPatch, bad_receipt: str,
) -> None:
    owner = resource_owner.owner
    assert owner is not None
    transport = resource_owner.transport
    method = f"create_{owner.kind}"

    async def disconnected(**_arguments: object):
        raise ConnectionError("请求未取得创建回执")

    with monkeypatch.context() as fault:
        fault.setattr(transport, method, disconnected)
        with pytest.raises(ConnectionError, match="请求未取得创建回执"):
            await create(operation)
    await operation.close()
    rid, = owner._store.ids()
    original, = resource_owner.leases.leases_for_resource(rid)
    owner = resource_owner.restart()
    current = await owner.get_resource(rid)
    assert current["reconciliation_status"] == "unknown" and current["available_actions"] == ["stop"]
    cancel = transport.cancel_creation
    calls = []

    async def receipt(resource_id: str, *, owner_revision: int):
        calls.append((resource_id, owner_revision))
        if bad_receipt == "none":
            raise ConnectionError("取消未取得回执")
        result = {"resource_id": resource_id, "workspace_id": owner.workspace_id, "owner_revision": owner_revision,
                  "outcome": "not_created"}
        result.update({"revision": {"owner_revision": owner_revision + 1}, "boolean": {"owner_revision": True},
                       "workspace": {"workspace_id": "wrong-workspace"}, "outcome": {"outcome": "unknown"},
                       "resource": {"resource": {}}}[bad_receipt])
        return result

    with monkeypatch.context() as fault:
        fault.setattr(transport, "cancel_creation", receipt)
        with pytest.raises(ResourceConflict, match="原 operation"):
            await owner.command(rid, action="stop", expected_revision=1,
                holder_id=original.turn_stream_id, operation_id=original.operation_id)
        assert not calls
        with pytest.raises(ConnectionError if bad_receipt == "none" else ValueError):
            await owner.command(rid, action="stop", expected_revision=1,
                holder_id="request:cancel-first", operation_id="cancel-first")
    assert calls == [(rid, 1)] and not transport.records
    pending = resource_owner.leases.get_lease(original.lease_id)
    assert pending is not None and pending.status == "reconcile_required"
    assert (await owner.get_resource(rid))["reconciliation_status"] == "unknown"
    result = await owner.command(rid, action="stop", expected_revision=1,
        holder_id="request:cancel-retry", operation_id="cancel-retry")
    assert result["status"] == "deleted" and result["reconciliation_status"] == "verified"
    assert result["thread_associations"] == [] and result["available_actions"] == []
    source = result["source_thread"]
    assert isinstance(source, dict) and source["session_id"] == SESSION_A
    assert not resource_owner.leases.leases_for_resource(rid)
    settled = resource_owner.leases.get_lease(original.lease_id)
    assert settled is not None and settled.status == "settled"
    assert settled.operation_id == original.operation_id and settled.turn_stream_id == original.turn_stream_id
    assert transport.creates == 0 and transport.deleted == []
    assert (await cancel(rid, owner_revision=1))["outcome"] == "not_created"
    cold = resource_owner.restart()
    assert (await cold.get_resource(rid))["status"] == "deleted"
    assert not await cold.list_resources()


@pytest.mark.parametrize("native_status", ["created", "running"])
async def test_unverified_native_start_keeps_creation_intent_until_management_cancellation(
    resource_owner: OwnerFixture, operation: Operation, monkeypatch: pytest.MonkeyPatch, native_status: str,
) -> None:
    owner = resource_owner.owner
    assert owner is not None
    transport = resource_owner.transport
    method = f"create_{owner.kind}"
    original_create = getattr(transport, method)
    kind = owner.kind

    async def starting(**arguments: object) -> dict[str, object]:
        runtime = await original_create(**arguments)
        rid = str(runtime[f"{kind}_id"])
        transport.records[rid].update(status=native_status, reconciliation_status="unknown")
        raise ConnectionError("原生创建仍在进行，Python未取得回执")

    with monkeypatch.context() as fault:
        fault.setattr(transport, method, starting)
        with pytest.raises(ConnectionError, match="Python未取得回执"):
            await create(operation)
    await operation.close()
    rid, = owner._store.ids()
    origin, = resource_owner.leases.leases_for_resource(rid)
    owner = resource_owner.restart()
    restored = await owner.get_resource(rid)
    assert restored["status"] == "creating" and restored["reconciliation_status"] == "unknown"
    assert restored["available_actions"] == ["stop"]
    stored = owner._store.get(rid)
    assert stored is not None and stored.status == "creating"
    revision = stored.owner_revision
    assert transport.records[rid]["status"] == native_status
    pending = resource_owner.leases.get_lease(origin.lease_id)
    assert pending is not None and pending.status == "reconcile_required"
    cancel, stop = transport.cancel_creation, transport.stop_resource
    calls: list[str] = []

    async def cancelling(resource_id: str, *, owner_revision: int) -> dict[str, object]:
        calls.append("cancel_creation")
        assert owner_revision == revision
        transport.records[resource_id].update(
            status="failed" if owner.kind == "browser" else "terminated", reconciliation_status="verified",
        )
        return await cancel(resource_id, owner_revision=owner_revision)

    async def stopping(resource_id: str, *, reason: str | None = None) -> dict[str, object]:
        calls.append("stop")
        assert calls == ["cancel_creation", "stop"]
        return await stop(resource_id, reason=reason)

    with monkeypatch.context() as fault:
        fault.setattr(transport, "cancel_creation", cancelling)
        fault.setattr(transport, "stop_resource", stopping)
        with pytest.raises(ResourceConflict, match="原 operation"):
            await owner.command(rid, action="stop", expected_revision=revision,
                holder_id=origin.turn_stream_id, operation_id=origin.operation_id)
        assert calls == []
        result = await owner.command(rid, action="stop", expected_revision=revision,
            holder_id="request:startup-cancel", operation_id="startup-cancel")
    assert result["status"] == ("closed" if owner.kind == "browser" else "terminated")
    assert result["reconciliation_status"] == "verified" and calls == ["cancel_creation", "stop"]
    assert transport.creates == 1 and not resource_owner.leases.leases_for_resource(rid)
    settled = resource_owner.leases.get_lease(origin.lease_id)
    assert settled is not None and settled.status == "settled"
    assert (settled.operation_id, settled.turn_stream_id) == (origin.operation_id, origin.turn_stream_id)
