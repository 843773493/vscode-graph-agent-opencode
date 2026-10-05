from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.abstractions.browser_resources import BrowserRequestError
from app.api.deps import (
    get_browser_resource_owner,
    get_terminal_resource_owner,
    verify_local_token,
)
from app.api.resources import router
from app.core.trace_middleware import TraceMiddleware
from tests.support.browser_owner import BrowserBundle, build_browser_bundle
from tests.support.terminal_owner import (
    CHILD,
    SESSION_A,
    THREAD_A,
    Bundle,
    build_terminal_bundle,
)


@pytest.fixture
def browser(tmp_path: Path) -> BrowserBundle:
    root = tmp_path / "browser"
    root.mkdir()
    return build_browser_bundle(root)


@pytest.fixture
def terminal(tmp_path: Path) -> Bundle:
    root = tmp_path / "terminal"
    root.mkdir()
    return build_terminal_bundle(root)


@pytest.fixture
def client(browser: BrowserBundle, terminal: Bundle) -> Iterator[TestClient]:
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_browser_resource_owner] = lambda: browser.owner
    application.dependency_overrides[get_terminal_resource_owner] = lambda: terminal.owner
    application.dependency_overrides[verify_local_token] = lambda: "authenticated"
    with TestClient(application) as client:
        yield client


def create(client: TestClient) -> dict[str, object]:
    response = client.post("/api/v1/resources/browsers", json={
        "session_id": SESSION_A, "thread_id": CHILD, "retention_scope": "thread",
    })
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    return response.json()["data"]


def test_browser_api_real_operation_lease_uses_trace_and_exact_thread(
    client: TestClient, browser: BrowserBundle, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = create(client)
    rid = resource["resource_id"]
    assert resource["thread_associations"] == [{"session_id": SESSION_A, "thread_id": CHILD}]
    observed: list[tuple[str, str]] = []
    perform = browser.transport.perform

    async def checked(resource_id: str, action: str, payload: dict[str, object], *, actor: str) -> dict[str, object]:
        lease, = browser.leases.leases_for_resource(resource_id)
        assert lease.holder_thread_id == CHILD
        observed.append((lease.turn_stream_id, lease.operation_id))
        return await perform(resource_id, action, payload, actor=actor)

    monkeypatch.setattr(browser.transport, "perform", checked)
    rejected = client.post(f"/api/v1/resources/{rid}/browser-operation", json={
        "action": "read", "expected_revision": 1,
        "thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
    })
    assert rejected.status_code == 409
    assert observed == []
    receipt = client.post(f"/api/v1/resources/{rid}/browser-operation", json={
        "action": "read", "expected_revision": 1, "thread": resource["source_thread"],
    })
    assert receipt.status_code == 200, receipt.text
    request_id = receipt.headers["X-Request-ID"]
    assert receipt.json()["request_id"] == request_id
    assert observed == [(f"request:{request_id}", request_id)]
    assert receipt.json()["data"]["browser"]["source_thread"] == resource["source_thread"]
    assert browser.leases.leases_for_resource(rid) == []


def test_browser_owner_api_union_grant_promote_and_unassociated_delete(client: TestClient) -> None:
    resource = create(client)
    rid = resource["resource_id"]
    listed = client.get("/api/v1/resources").json()["data"]["items"]
    assert [item["resource_id"] for item in listed] == [rid]
    missing = client.post(f"/api/v1/resources/{rid}/transport-grant", json={"expected_revision": 1})
    assert missing.status_code == 409
    granted = client.post(f"/api/v1/resources/{rid}/transport-grant", json={
        "expected_revision": 1, "thread": resource["source_thread"],
    })
    assert granted.status_code == 200, granted.text
    assert granted.json()["data"]["resource"] == resource
    assert granted.json()["data"]["grant_token"]
    promoted = client.post(f"/api/v1/resources/{rid}/promote", json={"expected_revision": 1}).json()["data"]
    detached = client.post(f"/api/v1/resources/{rid}/detach", json={
        "expected_revision": promoted["owner_revision"], "thread": resource["source_thread"],
    })
    assert detached.status_code == 200, detached.text
    owner = detached.json()["data"]
    assert owner["retention_scope"] == "workspace"
    assert owner["thread_associations"] == []
    assert owner["reclaim_at"] is None
    deleted = client.post(f"/api/v1/resources/{rid}/delete", json={"expected_revision": owner["owner_revision"]})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["status"] == "deleted"
    assert client.get("/api/v1/resources?kind=browser").json()["data"]["items"] == []


def test_owner_api_preserves_live_browser_title_and_terminal_title(
    client: TestClient, browser: BrowserBundle, terminal: Bundle,
) -> None:
    resource = create(client)
    rid = resource["resource_id"]
    browser.transport.records[rid]["title"] = "当前页面标题"
    terminal_response = client.post("/api/v1/resources/terminals", json={
        "session_id": SESSION_A, "title": "用户 Shell",
    })
    assert terminal_response.status_code == 200, terminal_response.text
    terminal_id = terminal_response.json()["data"]["resource_id"]
    terminal.transport.records[terminal_id]["title"] = "传输进程标题"
    listed = {item["resource_id"]: item for item in client.get("/api/v1/resources").json()["data"]["items"]}
    assert listed[rid]["metadata"]["title"] == "当前页面标题"
    assert listed[terminal_id]["metadata"]["title"] == "用户 Shell"
    browser.transport.records[rid]["title"] = "导航后的页面标题"
    assert client.get(f"/api/v1/resources/{rid}").json()["data"]["metadata"]["title"] == "导航后的页面标题"
    del browser.transport.records[rid]
    missing = client.get(f"/api/v1/resources/{rid}").json()["data"]
    assert missing["metadata"]["title"] == "用户浏览器"
    assert missing["reconciliation_status"] == "unknown"
    assert missing["available_actions"] == []


@pytest.mark.parametrize("state", ["freezing", "restoring"])
def test_failed_transition_receipt_blocks_following_operation(
    client: TestClient, browser: BrowserBundle, state: str,
) -> None:
    resource = create(client)
    rid = resource["resource_id"]
    browser.transport.records[rid].update(reconciliation_status="unknown", resource_state=state)
    browser.transport.failure = BrowserRequestError(method="POST", path="/freeze", status=500, payload={
        "error": "页面转换后状态未核实", "operation_outcome": "failed",
        "browser": browser.transport.records[rid],
    })
    command = {"action": "freeze", "expected_revision": 1, "thread": resource["source_thread"]}
    failed = client.post(f"/api/v1/resources/{rid}/browser-operation", json=command)
    assert failed.status_code == 500, failed.text
    discovered = client.get(f"/api/v1/resources/{rid}").json()["data"]
    assert discovered["metadata"]["resource_state"] == state
    assert discovered["reconciliation_status"] == "unknown"
    assert discovered["available_actions"] == []
    rejected = client.post(f"/api/v1/resources/{rid}/browser-operation", json={**command, "action": "run"})
    assert rejected.status_code == 409, rejected.text
    assert len(browser.transport.calls) == 1


def test_lost_browser_action_receipt_is_unknown_and_error_redacts_real_path(
    client: TestClient, browser: BrowserBundle,
) -> None:
    resource = create(client)
    rid = resource["resource_id"]
    browser.transport.failure = ConnectionError(f"回执丢失: {browser.root}/private/key")
    failed = client.post(f"/api/v1/resources/{rid}/browser-operation", json={
        "action": "run", "expected_revision": 1, "thread": resource["source_thread"], "payload": {},
    })
    assert failed.status_code == 503
    assert str(browser.root) not in failed.text
    observed = client.get(f"/api/v1/resources/{rid}").json()["data"]
    assert observed["reconciliation_status"] == "unknown"
    assert observed["available_actions"] == []
    assert browser.leases.leases_for_resource(rid) == []
