from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.support.resource_owners.sessions import (
    SESSION_A,
    THREAD_A,
    WORKSPACE_ID,
)
from tests.support.resource_owners.terminal import TerminalOwnerFixture


def _create_terminal_resource(resource_api_client: TestClient, *, scope: str = "thread") -> dict[str, object]:
    response = resource_api_client.post("/api/v1/resources/terminals", json={
        "session_id": SESSION_A, "thread_id": THREAD_A,
        "retention_scope": scope, "title": "真实管理请求",
    })
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    return response.json()["data"]


def test_create_and_control_use_trace_request_identity_for_actual_lease(
    resource_api_client: TestClient, terminal_owner_bundle: TerminalOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str]] = []
    create = terminal_owner_bundle.transport.create_terminal
    sync = terminal_owner_bundle.transport.sync_owner_revision

    def record(resource_id: str) -> None:
        lease, = terminal_owner_bundle.leases.leases_for_resource(resource_id)
        observed.append((lease.turn_stream_id, lease.operation_id))

    async def create_checked(**arguments: object) -> dict[str, object]:
        record(str(arguments["terminal_id"]))
        return await create(**arguments)

    async def sync_checked(resource_id: str, revision: int) -> None:
        record(resource_id)
        await sync(resource_id, revision)

    monkeypatch.setattr(terminal_owner_bundle.transport, "create_terminal", create_checked)
    monkeypatch.setattr(terminal_owner_bundle.transport, "sync_owner_revision", sync_checked)
    response = resource_api_client.post("/api/v1/resources/terminals", json={"session_id": SESSION_A})
    resource = response.json()["data"]
    resource_id = resource["resource_id"]
    promoted = resource_api_client.post(f"/api/v1/resources/{resource_id}/promote", json={"expected_revision": 1})
    assert promoted.status_code == 200
    assert promoted.json()["data"]["retention_scope"] == "workspace"
    for receipt, lease in zip([response, promoted], observed, strict=True):
        request_id = receipt.headers["X-Request-ID"]
        assert receipt.json()["request_id"] == request_id
        assert lease == (f"request:{request_id}", request_id)
    assert terminal_owner_bundle.leases.leases_for_resource(resource_id) == []


def test_api_conflict_keeps_owner_record_and_exact_associations(resource_api_client: TestClient, terminal_owner_bundle: TerminalOwnerFixture) -> None:
    resource = _create_terminal_resource(resource_api_client)
    resource_id = resource["resource_id"]
    conflict = resource_api_client.post(f"/api/v1/resources/{resource_id}/promote", json={"expected_revision": 99})
    assert conflict.status_code == 409
    lease = terminal_owner_bundle.leases.acquire_operation(
        resource_id=resource_id, turn_stream_id="actual-tool", operation_id="pending-read",
        holder_session_id=SESSION_A, holder_thread_id=THREAD_A,
    )
    blocked = resource_api_client.post(f"/api/v1/resources/{resource_id}/detach", json={
        "expected_revision": 1, "thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
    })
    assert blocked.status_code == 409
    unchanged = resource_api_client.get(f"/api/v1/resources/{resource_id}").json()["data"]
    assert unchanged["thread_associations"] == resource["thread_associations"]
    assert unchanged["owner_revision"] == 1
    terminal_owner_bundle.leases.release(lease.lease_id)


def test_api_unknown_delete_reports_503_without_leaking_path_or_disappearing(
    resource_api_client: TestClient, terminal_owner_bundle: TerminalOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _create_terminal_resource(resource_api_client)
    resource_id = resource["resource_id"]
    detached = resource_api_client.post(f"/api/v1/resources/{resource_id}/detach", json={
        "expected_revision": 1, "thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
    })
    assert detached.status_code == 200

    async def lose_receipt(terminal_id: str) -> dict[str, object]:
        raise ConnectionError(f"PTY 回执丢失: {terminal_owner_bundle.root}/private/key")

    monkeypatch.setattr(terminal_owner_bundle.transport, "delete_resource", lose_receipt)
    failed = resource_api_client.post(f"/api/v1/resources/{resource_id}/delete", json={"expected_revision": 2})
    assert failed.status_code == 503
    assert str(terminal_owner_bundle.root) not in failed.text
    listed = resource_api_client.get("/api/v1/resources").json()["data"]["items"]
    assert listed[0]["resource_id"] == resource_id
    assert listed[0]["reconciliation_status"] == "unknown"
    assert listed[0]["status"] != "deleted"


def test_terminal_api_delete_rejects_thread_association_without_side_effects(
    resource_api_client: TestClient, terminal_owner_bundle: TerminalOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _create_terminal_resource(resource_api_client)
    resource_id = created["resource_id"]
    response = resource_api_client.get(f"/api/v1/resources/{resource_id}")
    assert response.status_code == 200, response.text
    associated = response.json()["data"]
    assert associated["workspace_id"] == WORKSPACE_ID
    assert associated["kind"] == "terminal"
    assert associated["status"] == "running"
    assert associated["reconciliation_status"] == "verified"
    assert associated["thread_associations"] == [
        {"session_id": SESSION_A, "thread_id": THREAD_A},
    ]
    assert "delete" not in associated["available_actions"]

    transport_revision = terminal_owner_bundle.transport.revisions[resource_id]
    transport_status = terminal_owner_bundle.transport.records[resource_id]["status"]
    delete_calls: list[str] = []
    delete_resource = terminal_owner_bundle.transport.delete_resource

    async def record_delete(terminal_id: str) -> dict[str, object]:
        delete_calls.append(terminal_id)
        return await delete_resource(terminal_id)

    monkeypatch.setattr(terminal_owner_bundle.transport, "delete_resource", record_delete)
    rejected = resource_api_client.post(
        f"/api/v1/resources/{resource_id}/delete",
        json={"expected_revision": associated["owner_revision"]},
    )
    assert rejected.status_code == 409, rejected.text

    unchanged_response = resource_api_client.get(f"/api/v1/resources/{resource_id}")
    assert unchanged_response.status_code == 200, unchanged_response.text
    unchanged = unchanged_response.json()["data"]
    assert unchanged == associated
    assert terminal_owner_bundle.transport.revisions[resource_id] == transport_revision
    assert terminal_owner_bundle.transport.records[resource_id]["transport_owner_revision"] == transport_revision
    assert terminal_owner_bundle.transport.records[resource_id]["status"] == transport_status
    assert delete_calls == []
    assert terminal_owner_bundle.transport.deleted == []
    assert terminal_owner_bundle.leases.leases_for_resource(resource_id) == []

    association = associated["thread_associations"][0]
    detached_response = resource_api_client.post(
        f"/api/v1/resources/{resource_id}/detach",
        json={"expected_revision": associated["owner_revision"], "thread": association},
    )
    assert detached_response.status_code == 200, detached_response.text
    detached = detached_response.json()["data"]
    assert detached["thread_associations"] == []
    assert "delete" in detached["available_actions"]
    assert detached["owner_revision"] == associated["owner_revision"] + 1

    deleted_response = resource_api_client.post(
        f"/api/v1/resources/{resource_id}/delete",
        json={"expected_revision": detached["owner_revision"]},
    )
    assert deleted_response.status_code == 200, deleted_response.text
    deleted = deleted_response.json()["data"]
    assert deleted["status"] == "deleted"
    assert deleted["thread_associations"] == []
    assert deleted["owner_revision"] == detached["owner_revision"] + 1
    assert terminal_owner_bundle.transport.revisions[resource_id] == deleted["owner_revision"]
    assert terminal_owner_bundle.transport.records[resource_id]["status"] == "deleted"
    assert delete_calls == [resource_id]
    assert terminal_owner_bundle.transport.deleted == [resource_id]
    assert terminal_owner_bundle.leases.leases_for_resource(resource_id) == []
    listed = resource_api_client.get("/api/v1/resources", params={"kind": "terminal"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["items"] == []


def test_grant_requires_real_association_and_cannot_create_one(resource_api_client: TestClient) -> None:
    resource = _create_terminal_resource(resource_api_client)
    resource_id = resource["resource_id"]
    missing_thread = resource_api_client.post(f"/api/v1/resources/{resource_id}/transport-grant", json={"expected_revision": 1})
    assert missing_thread.status_code == 409
    granted = resource_api_client.post(f"/api/v1/resources/{resource_id}/transport-grant", json={
        "expected_revision": 1, "thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
    })
    assert granted.status_code == 200
    assert granted.json()["data"]["resource"]["thread_associations"] == resource["thread_associations"]
    assert granted.json()["data"]["grant_token"]
