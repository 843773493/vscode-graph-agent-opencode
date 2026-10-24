from __future__ import annotations

import errno
import json
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest

import pytest
from fastapi.testclient import TestClient

from app.abstractions.browser_resources import BrowserRequestError
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import (
    CHILD_THREAD_A,
    SESSION_A,
    THREAD_A,
    WORKSPACE_ID,
)
from tests.support.resource_owners.terminal import TerminalOwnerFixture


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("foreign_identity", [
    "/private/runtime-id",
    r"C:\private\runtime-id",
    r"\\server\share\private\runtime-id",
    r"\\?\UNC\server\share\private\runtime-id",
    None,
], ids=["posix", "drive", "unc", "extended-unc", "duplicate"])
def test_runtime_inventory_rejection_publishes_facts_without_foreign_identity(
    resource_api_client: TestClient, terminal_owner_bundle: TerminalOwnerFixture,
    browser_owner_bundle: BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, kind: str, foreign_identity: str | None,
) -> None:
    created = resource_api_client.post(f"/api/v1/resources/{kind}s", json={
        "session_id": SESSION_A, "thread_id": THREAD_A,
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    bundle = terminal_owner_bundle if kind == "terminal" else browser_owner_bundle
    resource_id = resource["resource_id"]
    original = dict(bundle.transport.records[resource_id])
    rejected_id = foreign_identity if foreign_identity is not None else resource_id
    reason = "unregistered_identity" if foreign_identity is not None else "duplicate_identity"
    rejected = {**original, f"{kind}_id": rejected_id}
    owner_file = bundle.root / ".boxteam" / f"{kind}-resources.json"
    lease_file = bundle.root / ".boxteam" / "resources.json"
    before = (owner_file.read_bytes(), lease_file.read_bytes())

    async def invalid_inventory() -> list[dict[str, object]]:
        return [dict(original), dict(rejected)]

    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "list_resources", invalid_inventory)
        failed = resource_api_client.get("/api/v1/resources", params={"kind": kind})
    assert failed.status_code == 500, failed.text
    assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
    message = failed.json()["message"]
    assert f"kind={kind}" in message and f"reason={reason}" in message
    assert "index=1" in message
    assert rejected_id not in message
    assert any(
        record.name == "app.services.business.workspace_resources.facts"
        and record.args == (kind, reason, 1, rejected_id)
        for record in caplog.records
    ), "原始身份应完整留在诊断日志参数中"
    assert (owner_file.read_bytes(), lease_file.read_bytes()) == before
    assert bundle.transport.records[resource_id] == original
    assert bundle.transport.creates == 1 and bundle.transport.deleted == []
    assert bundle.leases.leases_for_resource(resource_id) == []
    recovered = resource_api_client.get("/api/v1/resources", params={"kind": kind})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["data"]["items"] == [resource]



@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("entry", ["inventory", "single"])
@pytest.mark.parametrize("native_path", [
    "/private/runtime-cwd", r"C:\private\runtime-cwd", r"C:private\runtime-cwd",
    r"\private\runtime-cwd", r"\\server\share\private\runtime-cwd",
    r"\\?\UNC\server\share\private\runtime-cwd",
], ids=["posix", "drive", "drive-relative", "rooted", "unc", "extended-unc"])
def test_runtime_working_directory_rejects_native_root_without_changing_owner(
    resource_api_client: TestClient, terminal_owner_bundle: TerminalOwnerFixture,
    browser_owner_bundle: BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, kind: str, entry: str, native_path: str,
) -> None:
    created = resource_api_client.post(f"/api/v1/resources/{kind}s", json={
        "session_id": SESSION_A, "thread_id": THREAD_A,
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    bundle = terminal_owner_bundle if kind == "terminal" else browser_owner_bundle
    resource_id = resource["resource_id"]
    original = dict(bundle.transport.records[resource_id])
    owner_file = bundle.root / ".boxteam" / f"{kind}-resources.json"
    lease_file = bundle.root / ".boxteam" / "resources.json"
    before = (owner_file.read_bytes(), lease_file.read_bytes())

    async def invalid_inventory() -> list[dict[str, object]]:
        return [{**original, "cwd_relative": native_path}]

    request_id = f"cwd-rejection-{kind}-{entry}"
    headers = {"X-Request-ID": request_id}
    with monkeypatch.context() as patch:
        patch.setattr(bundle.transport, "list_resources", invalid_inventory)
        failed = (resource_api_client.get("/api/v1/resources", params={"kind": kind}, headers=headers)
                  if entry == "inventory" else resource_api_client.get(f"/api/v1/resources/{resource_id}", headers=headers))
    assert failed.status_code == 400, failed.text
    assert failed.headers["X-Request-ID"] == request_id
    assert failed.json()["detail"] == "ValueError: Node 运行时 工作目录不是工作区内相对目录"
    assert "runtime-cwd" not in failed.text
    assert any(record.name == "app.services.business.workspace_resources.facts"
               and record.args == (native_path,) for record in caplog.records)
    assert (owner_file.read_bytes(), lease_file.read_bytes()) == before
    assert bundle.transport.records[resource_id] == original
    assert bundle.transport.creates == 1 and bundle.transport.deleted == []
    assert bundle.leases.leases_for_resource(resource_id) == []
    recovered = resource_api_client.get(f"/api/v1/resources/{resource_id}")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["data"] == resource



@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("relative", [".", "app", "../external", r"..\external"])
def test_runtime_working_directory_preserves_relative_external_target(
    resource_api_client: TestClient, terminal_owner_bundle: TerminalOwnerFixture,
    browser_owner_bundle: BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
    kind: str, relative: str,
) -> None:
    created = resource_api_client.post(f"/api/v1/resources/{kind}s", json={
        "session_id": SESSION_A, "thread_id": THREAD_A,
    })
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    bundle = terminal_owner_bundle if kind == "terminal" else browser_owner_bundle
    original = dict(bundle.transport.records[resource["resource_id"]])

    async def relative_inventory() -> list[dict[str, object]]:
        return [{**original, "cwd_relative": relative}]

    monkeypatch.setattr(bundle.transport, "list_resources", relative_inventory)
    listed = resource_api_client.get("/api/v1/resources", params={"kind": kind})
    observed = resource_api_client.get(f"/api/v1/resources/{resource['resource_id']}")
    assert listed.status_code == observed.status_code == 200
    assert listed.json()["data"]["items"] == [observed.json()["data"]]
    assert observed.json()["data"]["metadata"]["cwd_relative"] == relative


def _create_browser_resource(resource_api_client: TestClient) -> dict[str, object]:
    response = resource_api_client.post("/api/v1/resources/browsers", json={
        "session_id": SESSION_A, "thread_id": CHILD_THREAD_A, "retention_scope": "thread",
    })
    assert response.status_code == 200, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    return response.json()["data"]


def test_browser_api_real_operation_lease_uses_trace_and_exact_thread(
    resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _create_browser_resource(resource_api_client)
    resource_id = resource["resource_id"]
    assert resource["thread_associations"] == [{"session_id": SESSION_A, "thread_id": CHILD_THREAD_A}]
    observed: list[tuple[str, str]] = []
    perform = browser_owner_bundle.transport.perform

    async def checked(resource_id: str, action: str, payload: dict[str, object], *, actor: str) -> dict[str, object]:
        lease, = browser_owner_bundle.leases.leases_for_resource(resource_id)
        assert lease.holder_thread_id == CHILD_THREAD_A
        observed.append((lease.turn_stream_id, lease.operation_id))
        return await perform(resource_id, action, payload, actor=actor)

    monkeypatch.setattr(browser_owner_bundle.transport, "perform", checked)
    rejected = resource_api_client.post(f"/api/v1/resources/{resource_id}/browser-operation", json={
        "action": "read", "expected_revision": 1,
        "thread": {"session_id": SESSION_A, "thread_id": THREAD_A},
    })
    assert rejected.status_code == 409
    assert observed == []
    receipt = resource_api_client.post(f"/api/v1/resources/{resource_id}/browser-operation", json={
        "action": "read", "expected_revision": 1, "thread": resource["source_thread"],
    })
    assert receipt.status_code == 200, receipt.text
    request_id = receipt.headers["X-Request-ID"]
    assert receipt.json()["request_id"] == request_id
    assert observed == [(f"request:{request_id}", request_id)]
    assert receipt.json()["data"]["browser"]["source_thread"] == resource["source_thread"]
    assert browser_owner_bundle.leases.leases_for_resource(resource_id) == []


def test_browser_owner_api_union_grant_promote_and_unassociated_delete(resource_api_client: TestClient) -> None:
    resource = _create_browser_resource(resource_api_client)
    resource_id = resource["resource_id"]
    listed = resource_api_client.get("/api/v1/resources").json()["data"]["items"]
    assert [item["resource_id"] for item in listed] == [resource_id]
    missing = resource_api_client.post(f"/api/v1/resources/{resource_id}/transport-grant", json={"expected_revision": 1})
    assert missing.status_code == 409
    granted = resource_api_client.post(f"/api/v1/resources/{resource_id}/transport-grant", json={
        "expected_revision": 1, "thread": resource["source_thread"],
    })
    assert granted.status_code == 200, granted.text
    assert granted.json()["data"]["resource"] == resource
    assert granted.json()["data"]["grant_token"]
    promoted = resource_api_client.post(f"/api/v1/resources/{resource_id}/promote", json={"expected_revision": 1}).json()["data"]
    detached = resource_api_client.post(f"/api/v1/resources/{resource_id}/detach", json={
        "expected_revision": promoted["owner_revision"], "thread": resource["source_thread"],
    })
    assert detached.status_code == 200, detached.text
    owner = detached.json()["data"]
    assert owner["retention_scope"] == "workspace"
    assert owner["thread_associations"] == []
    assert owner["reclaim_at"] is None
    deleted = resource_api_client.post(f"/api/v1/resources/{resource_id}/delete", json={"expected_revision": owner["owner_revision"]})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["status"] == "deleted"
    assert resource_api_client.get("/api/v1/resources?kind=browser").json()["data"]["items"] == []


def test_owner_api_preserves_live_browser_title_and_terminal_title(
    resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture, terminal_owner_bundle: TerminalOwnerFixture,
) -> None:
    resource = _create_browser_resource(resource_api_client)
    resource_id = resource["resource_id"]
    browser_owner_bundle.transport.records[resource_id]["title"] = "当前页面标题"
    terminal_response = resource_api_client.post("/api/v1/resources/terminals", json={
        "session_id": SESSION_A, "title": "用户 Shell",
    })
    assert terminal_response.status_code == 200, terminal_response.text
    terminal_id = terminal_response.json()["data"]["resource_id"]
    terminal_owner_bundle.transport.records[terminal_id]["title"] = "传输进程标题"
    listed = {item["resource_id"]: item for item in resource_api_client.get("/api/v1/resources").json()["data"]["items"]}
    assert listed[resource_id]["metadata"]["title"] == "当前页面标题"
    assert listed[terminal_id]["metadata"]["title"] == "用户 Shell"
    browser_owner_bundle.transport.records[resource_id]["title"] = "导航后的页面标题"
    assert resource_api_client.get(f"/api/v1/resources/{resource_id}").json()["data"]["metadata"]["title"] == "导航后的页面标题"
    del browser_owner_bundle.transport.records[resource_id]
    missing = resource_api_client.get(f"/api/v1/resources/{resource_id}").json()["data"]
    assert missing["metadata"]["title"] == "用户浏览器"
    assert missing["reconciliation_status"] == "unknown"
    assert missing["available_actions"] == []


@pytest.mark.parametrize("state", ["freezing", "restoring"])
def test_failed_transition_receipt_blocks_following_operation(
    resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture, state: str,
) -> None:
    resource = _create_browser_resource(resource_api_client)
    resource_id = resource["resource_id"]
    browser_owner_bundle.transport.records[resource_id].update(reconciliation_status="unknown", resource_state=state)
    browser_owner_bundle.transport.failure = BrowserRequestError(method="POST", path="/freeze", status=500, payload={
        "error": "页面转换后状态未核实", "operation_outcome": "failed",
        "browser": browser_owner_bundle.transport.records[resource_id],
    })
    command = {"action": "freeze", "expected_revision": 1, "thread": resource["source_thread"]}
    failed = resource_api_client.post(f"/api/v1/resources/{resource_id}/browser-operation", json=command)
    assert failed.status_code == 500, failed.text
    discovered = resource_api_client.get(f"/api/v1/resources/{resource_id}").json()["data"]
    assert discovered["metadata"]["resource_state"] == state
    assert discovered["reconciliation_status"] == "unknown"
    assert discovered["available_actions"] == []
    rejected = resource_api_client.post(f"/api/v1/resources/{resource_id}/browser-operation", json={**command, "action": "run"})
    assert rejected.status_code == 409, rejected.text
    assert len(browser_owner_bundle.transport.calls) == 1


@pytest.mark.parametrize("native_path", [
    "/private/resource.sqlite",
    r"C:\private\resource.sqlite",
    r"\\server\share\private\resource.sqlite",
    r"\\?\UNC\server\share\private\resource.sqlite",
])
@pytest.mark.parametrize("wrapped", [False, True], ids=["native", "url-reason"])
def test_lost_browser_action_receipt_keeps_native_error_in_diagnostics(
    resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture,
    native_path: str, wrapped: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    resource = _create_browser_resource(resource_api_client)
    resource_id = resource["resource_id"]
    native = ConnectionError(errno.EACCES, "回执丢失", native_path)
    error = URLError(native) if wrapped else native
    browser_owner_bundle.transport.failure = error
    failed = resource_api_client.post(f"/api/v1/resources/{resource_id}/browser-operation", json={
        "action": "run", "expected_revision": 1, "thread": resource["source_thread"], "payload": {},
    })
    assert failed.status_code == 503
    detail = failed.json()["detail"]
    assert type(error).__name__ in detail
    assert "resource.sqlite" not in detail
    assert "errno=13" in detail
    saved = json.loads((browser_owner_bundle.root / ".boxteam" / "browser-resources.json").read_text())
    assert saved["records"][0]["error"] == detail
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)
    assert str(error) in caplog.text

    browser_owner_bundle.restart()
    observed = resource_api_client.get(f"/api/v1/resources/{resource_id}")
    listed = resource_api_client.get("/api/v1/resources", params={"kind": "browser"})
    assert observed.status_code == listed.status_code == 200
    item = observed.json()["data"]
    assert item["error"] == detail
    assert item["reconciliation_status"] == "unknown"
    assert item["available_actions"] == []
    assert listed.json()["data"]["items"] == [item]
    assert len(browser_owner_bundle.transport.calls) == 1
    assert browser_owner_bundle.leases.leases_for_resource(resource_id) == []


def test_transport_failure_preserves_api_route_without_treating_it_as_native_path(
    resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _create_browser_resource(resource_api_client)
    resource_id = resource["resource_id"]
    transport = BrowserManagerClient(workspace_id=WORKSPACE_ID, backend_url="http://browser.test")
    attempted: list[str] = []

    def rejected(request: UrlRequest, **_options: object) -> None:
        attempted.append(request.full_url)
        raise HTTPError(request.full_url, 500, "failure", {}, BytesIO(b'{"code":"browser_read_failed"}'))

    monkeypatch.setattr("app.services.infrastructure.browser_manager_client.urlopen", rejected)
    monkeypatch.setattr(browser_owner_bundle.transport, "perform", transport.perform)
    command = {"action": "read", "expected_revision": 1, "thread": resource["source_thread"]}
    failed = resource_api_client.post(f"/api/v1/resources/{resource_id}/browser-operation", json=command)
    assert failed.status_code == 500, failed.text
    assert failed.headers["X-Request-ID"]
    detail = failed.json()["detail"]
    assert detail == (
        f"BrowserRequestError: 浏览器管理器请求失败: method=GET, path=/api/browsers/{resource_id}/read, "
        "status=500 (code=browser_read_failed)"
    )
    saved = json.loads((browser_owner_bundle.root / ".boxteam" / "browser-resources.json").read_text())
    assert saved["records"][0]["error"] == detail
    browser_owner_bundle.restart()
    observed = resource_api_client.get(f"/api/v1/resources/{resource_id}").json()["data"]
    assert observed["error"] == detail
    assert observed["reconciliation_status"] == "unknown"
    assert observed["available_actions"] == []
    assert attempted == [f"http://browser.test/api/browsers/{resource_id}/read"]
    assert browser_owner_bundle.leases.leases_for_resource(resource_id) == []


def test_transport_timeout_api_preserves_recovery_details_and_durable_unknown(
    resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = _create_browser_resource(resource_api_client)
    resource_id = resource["resource_id"]
    transport = BrowserManagerClient(workspace_id=WORKSPACE_ID, backend_url="http://browser.test")
    attempted: list[str] = []

    def timed_out(request: UrlRequest, **_options: object) -> None:
        attempted.append(request.full_url)
        raise TimeoutError("timed out")

    monkeypatch.setattr("app.services.infrastructure.browser_manager_client.urlopen", timed_out)
    monkeypatch.setattr(browser_owner_bundle.transport, "perform", transport.perform)
    command = {"action": "run", "payload": {"timeoutMs": 25},
               "expected_revision": resource["owner_revision"], "thread": resource["source_thread"]}
    failed = resource_api_client.post(f"/api/v1/resources/{resource_id}/browser-operation", json=command)
    assert failed.status_code == 408, failed.text
    detail = failed.json()["detail"]
    assert "浏览器管理器操作超时" in detail
    assert "timeoutMs=25" in detail
    assert "code=browser_tool_timeout" in detail
    assert "retryable=true" in detail
    assert "recovery=page_reset" in detail
    assert failed.headers["X-Request-ID"]
    browser_owner_bundle.restart()
    observed = resource_api_client.get(f"/api/v1/resources/{resource_id}").json()["data"]
    assert observed["error"] == detail
    assert observed["reconciliation_status"] == "unknown"
    assert observed["available_actions"] == []
    assert browser_owner_bundle.leases.leases_for_resource(resource_id) == []
    rejected = resource_api_client.post(f"/api/v1/resources/{resource_id}/browser-operation", json=command)
    assert rejected.status_code == 409, rejected.text
    assert attempted == [f"http://browser.test/api/browsers/{resource_id}/run"]


def test_browser_api_delete_rejects_thread_association_without_side_effects(
    resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _create_browser_resource(resource_api_client)
    resource_id = created["resource_id"]
    response = resource_api_client.get(f"/api/v1/resources/{resource_id}")
    assert response.status_code == 200, response.text
    associated = response.json()["data"]
    assert associated["workspace_id"] == WORKSPACE_ID
    assert associated["kind"] == "browser"
    assert associated["status"] == "running"
    assert associated["reconciliation_status"] == "verified"
    assert associated["thread_associations"] == [created["source_thread"]]
    assert "delete" not in associated["available_actions"]

    transport_revision = browser_owner_bundle.transport.records[resource_id]["transport_owner_revision"]
    transport_status = browser_owner_bundle.transport.records[resource_id]["status"]
    delete_calls: list[str] = []
    delete_resource = browser_owner_bundle.transport.delete_resource

    async def record_delete(browser_id: str) -> dict[str, object]:
        delete_calls.append(browser_id)
        return await delete_resource(browser_id)

    monkeypatch.setattr(browser_owner_bundle.transport, "delete_resource", record_delete)
    rejected = resource_api_client.post(
        f"/api/v1/resources/{resource_id}/delete",
        json={"expected_revision": associated["owner_revision"]},
    )
    assert rejected.status_code == 409, rejected.text

    unchanged_response = resource_api_client.get(f"/api/v1/resources/{resource_id}")
    assert unchanged_response.status_code == 200, unchanged_response.text
    unchanged = unchanged_response.json()["data"]
    assert unchanged == associated
    assert browser_owner_bundle.transport.records[resource_id]["transport_owner_revision"] == transport_revision
    assert browser_owner_bundle.transport.records[resource_id]["status"] == transport_status
    assert delete_calls == []
    assert browser_owner_bundle.transport.deleted == []
    assert browser_owner_bundle.leases.leases_for_resource(resource_id) == []

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
    assert browser_owner_bundle.transport.records[resource_id]["transport_owner_revision"] == detached["owner_revision"]

    deleted_response = resource_api_client.post(
        f"/api/v1/resources/{resource_id}/delete",
        json={"expected_revision": detached["owner_revision"]},
    )
    assert deleted_response.status_code == 200, deleted_response.text
    deleted = deleted_response.json()["data"]
    assert deleted["status"] == "deleted"
    assert deleted["thread_associations"] == []
    assert deleted["owner_revision"] == detached["owner_revision"] + 1
    assert browser_owner_bundle.transport.records[resource_id]["transport_owner_revision"] == deleted["owner_revision"]
    assert browser_owner_bundle.transport.records[resource_id]["status"] == "deleted"
    assert delete_calls == [resource_id]
    assert browser_owner_bundle.transport.deleted == [resource_id]
    assert browser_owner_bundle.leases.leases_for_resource(resource_id) == []
    listed = resource_api_client.get("/api/v1/resources", params={"kind": "browser"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["items"] == []
