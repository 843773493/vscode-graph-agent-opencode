"""损坏的 Browser 响应不能成为普通 API 或持久错误中的原生位置。"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.support.resource_owners.browser import BrowserOwnerFixture
from tests.support.resource_owners.sessions import SESSION_A, WORKSPACE_ID


@pytest.mark.parametrize("native_path", [
    "/private/browser/state.json", r"C:\private\browser\state.json",
    r"\\server\share\browser\state.json", r"\\?\UNC\server\share\browser\state.json",
])
@pytest.mark.parametrize("failure, stage, status", [
    ("http", None, 500), ("http_scalar", None, 500),
    ("json", "json", 400), ("utf8", "json", 400),
    ("response", "response", 500), ("data", "record.data", 500),
    ("browser", "envelope.browser", 500), ("status", "browser", 500),
    ("missing_status", "browser", 400), ("status_type", "browser", 400),
])
def test_malformed_response_keeps_original_diagnostics_and_durable_unknown(
    resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    native_path: str, failure: str, stage: str | None, resource_http, status: int,
) -> None:
    created = resource_api_client.post("/api/v1/resources/browsers", json={"session_id": SESSION_A})
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    rid = resource["resource_id"]
    if stage == "browser":
        diagnostic: object = {
            **browser_owner_bundle.transport.records[rid], "page_id": "page_internal", "status": native_path,
        }
        if failure == "missing_status":
            del diagnostic["status"]
            diagnostic["diagnostic"] = native_path
        elif failure == "status_type":
            diagnostic["status"] = [native_path]
        response: object = {"data": {"browser": diagnostic}}
    elif failure == "data":
        diagnostic = response = {"data": native_path}
    elif failure == "browser":
        response = {"data": {"browser": native_path}}
        diagnostic = response["data"]
    else:
        diagnostic = response = [native_path]
    body = json.dumps(response, ensure_ascii=False).encode()
    if failure in {"http", "http_scalar"}:
        body = (native_path if failure == "http" else json.dumps(native_path)).encode()
        diagnostic = body.decode()
    elif failure in {"json", "utf8"}:
        body += b" trailing" if failure == "json" else b"\xff"
        diagnostic = body
    attempts: list[tuple[str, str]] = []

    def malformed(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == 30
        attempts.append((request.method, str(request.url)))
        if failure in {"http", "http_scalar"}:
            return httpx.Response(502, content=body)
        return httpx.Response(200, content=body)

    resource_http(malformed)
    transport = BrowserManagerClient(workspace_id=WORKSPACE_ID, backend_url="http://browser.test")
    monkeypatch.setattr(browser_owner_bundle.transport, "perform", transport.perform)
    command = {"action": "read", "expected_revision": resource["owner_revision"], "thread": resource["source_thread"]}
    with caplog.at_level(logging.ERROR):
        failed = resource_api_client.post(f"/api/v1/resources/{rid}/browser-operation", json=command)
    assert failed.status_code == status, failed.text
    exception_type = "ValueError" if status == 400 else "TypeError"
    expected = (
        "RuntimeError: 浏览器管理器请求失败: method=GET, status=502；请查看工作区浏览器服务日志"
        if stage is None else
        f"{exception_type}: 浏览器管理器响应协议错误: stage={stage}；请查看工作区浏览器服务日志"
    )
    if status == 400:
        assert failed.json() == {"detail": expected}
        assert failed.headers["X-Request-ID"]
    else:
        assert failed.json()["message"] == expected
        assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
    diagnostics = [
        record for record in caplog.records
        if record.name == "app.services.infrastructure.browser_manager_client"
        and isinstance(record.args, tuple) and record.args[-1] == diagnostic
    ]
    assert len(diagnostics) == 1
    cause_type = {
        "http": httpx.HTTPStatusError, "http_scalar": httpx.HTTPStatusError, "json": json.JSONDecodeError,
        "utf8": UnicodeDecodeError, "status": TypeError,
        "missing_status": ValueError, "status_type": ValueError,
    }.get(failure)
    if cause_type is not None:
        assert diagnostics[0].exc_info is not None
        cause = diagnostics[0].exc_info[1]
        assert type(cause) is cause_type
        assert any(
            record.exc_info is not None and record.exc_info[1].__cause__ is cause
            for record in caplog.records if record.name == "app.services.business.workspace_resources.owner"
        )
    browser_owner_bundle.restart()
    observed = resource_api_client.get(f"/api/v1/resources/{rid}")
    assert observed.status_code == 200, observed.text
    current = observed.json()["data"]
    assert current["error"] == expected
    assert current["reconciliation_status"] == "unknown"
    assert current["available_actions"] == []
    listed = resource_api_client.get("/api/v1/resources?kind=browser")
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["items"] == [current]
    saved = json.loads((browser_owner_bundle.root / ".boxteam/browser-resources.json").read_text())
    assert saved["records"][0]["error"] == expected
    pending, = browser_owner_bundle.leases.leases_for_resource(rid)
    assert pending.status == "reconcile_required"
    retry = resource_api_client.post(f"/api/v1/resources/{rid}/browser-operation", json=command)
    assert retry.status_code == 409, retry.text
    assert attempts == [("GET", f"http://browser.test/api/browsers/{rid}/read")]


@pytest.mark.parametrize("field, value", [
    (None, None), ("resource_id", "browser_other"), ("owner_revision", 0),
    ("owner_revision", True), ("grant_token", ""), ("grant_token", None), ("expires_at", ""),
])
def test_malformed_successful_grant_marks_unknown_before_api_serialization(
    resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, field: str | None, value: object,
    resource_http,
) -> None:
    created = resource_api_client.post("/api/v1/resources/browsers", json={"session_id": SESSION_A})
    assert created.status_code == 200, created.text
    resource = created.json()["data"]
    rid = resource["resource_id"]
    grant = {
        "resource_id": rid, "owner_revision": resource["owner_revision"],
        "grant_token": "/private/secret-token", "expires_at": "2026-10-23T00:00:00Z",
    }
    if field is None:
        grant.clear()
    else:
        grant[field] = value
    attempts: list[tuple[str, str]] = []

    def malformed(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == 30
        attempts.append((request.method, str(request.url)))
        return httpx.Response(200, json={"data": grant})

    resource_http(malformed)
    transport = BrowserManagerClient(workspace_id=WORKSPACE_ID, backend_url="http://browser.test")
    monkeypatch.setattr(browser_owner_bundle.transport, "issue_transport_grant", transport.issue_transport_grant)
    command = {"expected_revision": resource["owner_revision"], "thread": resource["source_thread"]}
    with caplog.at_level(logging.ERROR):
        failed = resource_api_client.post(f"/api/v1/resources/{rid}/transport-grant", json=command)
    assert failed.status_code == 500, failed.text
    expected = "TypeError: 浏览器管理器响应协议错误: stage=grant.receipt；请查看工作区浏览器服务日志"
    assert failed.json()["message"] == expected
    assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
    assert any(isinstance(record.args, tuple) and record.args[-1] == grant for record in caplog.records)
    browser_owner_bundle.restart()
    current = resource_api_client.get(f"/api/v1/resources/{rid}").json()["data"]
    assert current["error"] == expected
    assert current["reconciliation_status"] == "unknown"
    assert current["available_actions"] == []
    pending, = browser_owner_bundle.leases.leases_for_resource(rid)
    assert pending.status == "reconcile_required"
    retried = resource_api_client.post(f"/api/v1/resources/{rid}/transport-grant", json=command)
    assert retried.status_code == 409, retried.text
    assert attempts == [("POST", f"http://browser.test/api/browsers/{rid}/transport-grant")]


def test_empty_revision_receipt_cannot_finish_owner_restart_recovery(
    resource_api_client: TestClient, browser_owner_bundle: BrowserOwnerFixture,
    monkeypatch: pytest.MonkeyPatch,
    resource_http,
) -> None:
    created = resource_api_client.post("/api/v1/resources/browsers", json={"session_id": SESSION_A})
    assert created.status_code == 200, created.text
    rid = created.json()["data"]["resource_id"]
    transport = BrowserManagerClient(workspace_id=WORKSPACE_ID, backend_url="http://browser.test")
    attempts: list[str] = []

    def malformed(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        return httpx.Response(200, json={"data": {}})

    resource_http(malformed)
    sync = browser_owner_bundle.transport.sync_owner_revision
    monkeypatch.setattr(browser_owner_bundle.transport, "sync_owner_revision", transport.sync_owner_revision)
    browser_owner_bundle.restart()
    failed = resource_api_client.get(f"/api/v1/resources/{rid}")
    expected = "ValueError: 浏览器管理器响应协议错误: stage=browser；请查看工作区浏览器服务日志"
    assert failed.status_code == 400, failed.text
    assert failed.json() == {"detail": expected}
    saved = json.loads((browser_owner_bundle.root / ".boxteam/browser-resources.json").read_text())
    assert saved["records"][0]["reconciliation_status"] == "unknown"
    assert saved["records"][0]["error"] == expected
    assert browser_owner_bundle.owner is not None
    assert browser_owner_bundle.owner._recovered is False
    assert attempts == [f"http://browser.test/api/browsers/{rid}/owner-revision"]
    monkeypatch.setattr(browser_owner_bundle.transport, "sync_owner_revision", sync)
    recovered = resource_api_client.get(f"/api/v1/resources/{rid}")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["data"]["reconciliation_status"] == "unknown"
