from __future__ import annotations

import logging
from unittest.mock import Mock

import httpx
import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.abstractions.workspace_resources import ResourceTransportGrantRejected
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.config_service import ConfigService
from tests.support.resource_owners.sessions import WORKSPACE_ID


@pytest.fixture
def browser_manager_client() -> BrowserManagerClient:
    return BrowserManagerClient(workspace_id=WORKSPACE_ID, backend_url="http://browser.test")


@pytest.mark.asyncio
async def test_owner_client_sends_workspace_owner_and_per_call_actor_headers(
    browser_manager_client: BrowserManagerClient, resource_http,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"summary": "ok"}})

    resource_http(respond)
    assert await browser_manager_client._json_request("GET", "/api/browsers/browser_1/read", actor="agent") == {
        "data": {"summary": "ok"},
    }
    request, = requests
    assert request.headers["X-BoxTeam-Actor"] == "agent"
    assert request.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
    assert request.headers["X-BoxTeam-Resource-Owner"] == "workspace-browser"


@pytest.mark.asyncio
async def test_locked_error_is_reported_as_clean_tool_message(
    browser_manager_client: BrowserManagerClient, resource_http,
) -> None:
    resource_http(lambda request: httpx.Response(423, json={
        "code": "browser_agent_access_locked",
        "error": "用户锁定了浏览器，你暂时不能操作这个页面: browser_id=browser_1",
    }))
    with pytest.raises(RuntimeError, match="用户锁定了浏览器"):
        await browser_manager_client._json_request("GET", "/api/browsers/browser_1/read")


@pytest.mark.asyncio
async def test_timeout_error_preserves_retry_metadata(
    browser_manager_client: BrowserManagerClient, resource_http,
) -> None:
    resource_http(lambda request: httpx.Response(408, json={
        "code": "browser_tool_timeout",
        "error": "Playwright 代码执行超时: 20000ms；浏览器页面已重置，可重试",
        "retryable": True, "recovery": "page_reset", "timeout_ms": 20_000,
    }))
    with pytest.raises(BrowserRequestError, match="可重试") as error_info:
        await browser_manager_client._json_request("POST", "/api/browsers/browser_1/run", {})
    error = error_info.value
    assert error.status == 408 and error.code == "browser_tool_timeout"
    assert error.retryable is True and error.recovery == "page_reset"
    assert error.timeout_ms == 20_000


@pytest.mark.asyncio
@pytest.mark.parametrize("path, payload, expected", [
    ("/api/browsers/browser_1/run", {"code": "await page.waitForTimeout(53000);", "timeoutMs": 60000}, 70),
    ("/api/browsers/browser_1/read", None, 30),
])
async def test_request_timeout_covers_declared_browser_budget(
    browser_manager_client: BrowserManagerClient, resource_http, path: str, payload: dict[str, object] | None, expected: int,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == expected
        return httpx.Response(200, json={"data": {"result": "ok"}})

    resource_http(respond)
    result = await browser_manager_client._json_request("GET" if payload is None else "POST", path, payload)
    assert result == {"data": {"result": "ok"}}


@pytest.mark.asyncio
async def test_browser_backend_url_is_resolved_from_current_config_snapshot(resource_http) -> None:
    config = Mock(spec=ConfigService)
    config.get_browser_backend_url.side_effect = ["http://browser-a", "http://browser-b"]
    client = BrowserManagerClient(workspace_id=WORKSPACE_ID, config_service=config)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"summary": "ok"}})

    resource_http(respond)
    await client._json_request("GET", "/api/browsers")
    await client._json_request("GET", "/api/browsers")
    assert [str(request.url) for request in requests] == ["http://browser-a/api/browsers", "http://browser-b/api/browsers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation, data, stage", [
    ("list", "/private/browser", "list.data"),
    ("list", ["/private/browser"], "list.data"),
    ("grant", "/private/browser", "record.data"),
])
async def test_malformed_list_and_grant_fail_at_protocol_boundary(
    browser_manager_client: BrowserManagerClient, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, operation: str, data: object, stage: str, resource_http,
) -> None:
    response = {"data": data}
    resource_http(lambda request: httpx.Response(200, json=response))
    with caplog.at_level(logging.ERROR), pytest.raises(TypeError) as caught:
        if operation == "list":
            await browser_manager_client.list_resources()
        else:
            await browser_manager_client.issue_transport_grant("browser_1", owner_revision=1, association=None)
    assert str(caught.value) == f"浏览器管理器响应协议错误: stage={stage}；请查看工作区浏览器服务日志"
    assert any(isinstance(record.args, tuple) and record.args[-1] == response for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["file:///private/page.html", "file:///C:/Users/owner/page.html", "file://server/share/page.html"])
async def test_valid_local_page_url_survives_the_same_response_decoder(
    browser_manager_client: BrowserManagerClient, monkeypatch: pytest.MonkeyPatch, url: str, resource_http,
) -> None:
    record = {
        "browser_id": "browser_1", "page_id": "page_1", "workspace_id": WORKSPACE_ID,
        "status": "running", "transport_owner_revision": 1, "reconciliation_status": "verified",
        "url": url, "pages": [{"page_id": "page_1", "url": url, "active": True}],
    }
    resource_http(lambda request: httpx.Response(200, json={"data": record}))
    assert await browser_manager_client.get_resource("browser_1") == record


@pytest.fixture
def revision_receipt() -> dict[str, object]:
    return {
        "browser_id": "browser_1", "page_id": "page_1", "workspace_id": WORKSPACE_ID,
        "status": "running", "transport_owner_revision": 2, "reconciliation_status": "verified",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field, value", [
    ("browser_id", "browser_other"), ("workspace_id", "00000000-0000-4000-8000-000000000002"),
    ("transport_owner_revision", 1), ("transport_owner_revision", 3),
])
async def test_revision_sync_rejects_a_snapshot_for_another_identity_or_version(
    browser_manager_client: BrowserManagerClient, revision_receipt: dict[str, object],
    monkeypatch: pytest.MonkeyPatch, field: str, value: object, resource_http,
) -> None:
    revision_receipt[field] = value
    resource_http(lambda request: httpx.Response(200, json={"data": revision_receipt}))
    with pytest.raises(TypeError, match="stage=owner_revision.receipt"):
        await browser_manager_client.sync_owner_revision("browser_1", 2)


@pytest.mark.asyncio
async def test_revision_sync_consumes_the_complete_successful_snapshot(
    browser_manager_client: BrowserManagerClient, revision_receipt: dict[str, object], monkeypatch: pytest.MonkeyPatch, resource_http,
) -> None:
    resource_http(lambda request: httpx.Response(200, json={"data": revision_receipt}))
    await browser_manager_client.sync_owner_revision("browser_1", 2)


@pytest.mark.asyncio
async def test_async_transport_timeout_becomes_retryable_browser_error(
    browser_manager_client: BrowserManagerClient, resource_http,
) -> None:
    def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    resource_http(timed_out)
    with pytest.raises(BrowserRequestError, match="浏览器管理器操作超时") as error_info:
        await browser_manager_client.perform("browser_1", "read", {}, actor="agent")
    error = error_info.value
    assert error.status == 408 and error.code == "browser_tool_timeout"
    assert error.retryable is True and error.recovery == "page_reset"
    assert error.timeout_ms == 30_000


@pytest.fixture
def grant_rejection_payload() -> dict[str, object]:
    return {
        "code": "browser_transport_grant_rejected", "operation_outcome": "rejected",
        "error": "Browser 传输版本或运行状态冲突",
        "browser": {
            "browser_id": "browser_test", "page_id": "page_test", "workspace_id": WORKSPACE_ID,
            "status": "closed", "transport_owner_revision": 2, "reconciliation_status": "verified",
        },
    }


@pytest.mark.asyncio
async def test_valid_grant_rejection_has_explicit_transport_type(
    grant_rejection_payload: dict[str, object],
    browser_manager_client: BrowserManagerClient, resource_http,
) -> None:
    resource_http(lambda request: httpx.Response(409, json=grant_rejection_payload))
    with pytest.raises(ResourceTransportGrantRejected) as rejected:
        await browser_manager_client.issue_transport_grant("browser_test", owner_revision=2, association=None)
    assert rejected.value.snapshot == grant_rejection_payload["browser"]


@pytest.mark.parametrize(("field", "value"), [
    ("browser_id", "browser_other"),
    ("workspace_id", "00000000-0000-4000-8000-000000000002"),
    ("reconciliation_status", "unknown"),
    ("transport_owner_revision", 1),
    ("transport_owner_revision", True),
    ("status", "running"),
    ("code", "another_conflict"),
    ("operation_outcome", "unknown"),
    ("error", ""),
])
@pytest.mark.asyncio
async def test_untrusted_409_is_not_deterministic_grant_rejection(
    grant_rejection_payload: dict[str, object],
    field: str,
    value: object,
    browser_manager_client: BrowserManagerClient, resource_http,
) -> None:
    target = grant_rejection_payload if field in {"code", "operation_outcome", "error"} else grant_rejection_payload["browser"]
    target[field] = value
    resource_http(lambda request: httpx.Response(409, json=grant_rejection_payload))
    with pytest.raises((BrowserRequestError, ValueError, TypeError)):
        await browser_manager_client.issue_transport_grant("browser_test", owner_revision=2, association=None)
