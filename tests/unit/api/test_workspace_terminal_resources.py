from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_browser_resource_owner,
    get_terminal_resource_owner,
    verify_local_token,
)
from app.api.resources import router
from app.core.trace_middleware import TraceMiddleware
from tests.support.browser_owner import BrowserBundle, build_browser_bundle
from tests.support.terminal_owner import (
    SESSION_A,
    THREAD_A,
    Bundle,
    build_terminal_bundle,
)


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    return build_terminal_bundle(tmp_path)


@pytest.fixture
def client(bundle: Bundle, browser_bundle: BrowserBundle) -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_terminal_resource_owner] = lambda: bundle.owner
    app.dependency_overrides[get_browser_resource_owner] = lambda: browser_bundle.owner
    app.dependency_overrides[verify_local_token] = lambda: "authenticated-test"
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def browser_bundle(tmp_path: Path) -> BrowserBundle:
    root = tmp_path / "browser-owner"
    root.mkdir()
    return build_browser_bundle(root)


def _create(client: TestClient, *, scope: str = "thread") -> dict[str, object]:
    response = client.post("/api/v1/resources/terminals", json={
        "session_id": SESSION_A, "thread_id": THREAD_A,
        "retention_scope": scope, "title": "真实管理请求",
    })
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    return response.json()["data"]


def test_create_and_control_use_trace_request_identity_for_actual_lease(
    client: TestClient, bundle: Bundle, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str]] = []
    create = bundle.transport.create_terminal
    sync = bundle.transport.sync_owner_revision

    def record(rid: str) -> None:
        lease, = bundle.leases.leases_for_resource(rid)
        observed.append((lease.turn_stream_id, lease.operation_id))

    async def create_checked(**arguments: object) -> dict[str, object]:
        record(str(arguments["terminal_id"]))
        return await create(**arguments)

    async def sync_checked(rid: str, revision: int) -> None:
        record(rid)
        await sync(rid, revision)

    monkeypatch.setattr(bundle.transport, "create_terminal", create_checked)
    monkeypatch.setattr(bundle.transport, "sync_owner_revision", sync_checked)
    response = client.post("/api/v1/resources/terminals", json={"session_id": SESSION_A})
    resource = response.json()["data"]
    rid = resource["resource_id"]
    promoted = client.post(f"/api/v1/resources/{rid}/promote", json={"expected_revision": 1})
    assert promoted.status_code == 200
    assert promoted.json()["data"]["retention_scope"] == "workspace"
    for receipt, lease in zip([response, promoted], observed, strict=True):
        request_id = receipt.headers["X-Request-ID"]
        assert receipt.json()["request_id"] == request_id
        assert lease == (f"request:{request_id}", request_id)
    assert bundle.leases.leases_for_resource(rid) == []


def test_api_conflict_keeps_owner_record_and_exact_associations(client: TestClient, bundle: Bundle) -> None:
    resource = _create(client)
    rid = resource["resource_id"]
    conflict = client.post(f"/api/v1/resources/{rid}/promote", json={"expected_revision": 99})
    assert conflict.status_code == 409
    lease = bundle.leases.acquire_operation(
        resource_id=rid, turn_stream_id="actual-tool", operation_id="pending-read",
        holder_session_id=SESSION_A, holder_thread_id=THREAD_A,
    )
    blocked = client.post(f"/api/v1/resources/{rid}/detach", json={
        "expected_revision": 1, "thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
    })
    assert blocked.status_code == 409
    unchanged = client.get(f"/api/v1/resources/{rid}").json()["data"]
    assert unchanged["thread_associations"] == resource["thread_associations"]
    assert unchanged["owner_revision"] == 1
    bundle.leases.release(lease.lease_id)


def test_api_unknown_delete_reports_503_without_leaking_path_or_disappearing(
    client: TestClient, bundle: Bundle, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _create(client)
    rid = resource["resource_id"]
    detached = client.post(f"/api/v1/resources/{rid}/detach", json={
        "expected_revision": 1, "thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
    })
    assert detached.status_code == 200

    async def lose_receipt(terminal_id: str) -> dict[str, object]:
        raise ConnectionError(f"PTY 回执丢失: {bundle.root}/private/key")

    monkeypatch.setattr(bundle.transport, "delete_resource", lose_receipt)
    failed = client.post(f"/api/v1/resources/{rid}/delete", json={"expected_revision": 2})
    assert failed.status_code == 503
    assert str(bundle.root) not in failed.text
    listed = client.get("/api/v1/resources").json()["data"]["items"]
    assert listed[0]["resource_id"] == rid
    assert listed[0]["reconciliation_status"] == "unknown"
    assert listed[0]["status"] != "deleted"


def test_grant_requires_real_association_and_cannot_create_one(client: TestClient) -> None:
    resource = _create(client)
    rid = resource["resource_id"]
    missing_thread = client.post(f"/api/v1/resources/{rid}/transport-grant", json={"expected_revision": 1})
    assert missing_thread.status_code == 409
    granted = client.post(f"/api/v1/resources/{rid}/transport-grant", json={
        "expected_revision": 1, "thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
    })
    assert granted.status_code == 200
    assert granted.json()["data"]["resource"]["thread_associations"] == resource["thread_associations"]
    assert granted.json()["data"]["grant_token"]
