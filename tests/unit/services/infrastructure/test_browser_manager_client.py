from __future__ import annotations

import json
from io import BytesIO
from typing import Self
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.abstractions.workspace_resources import ResourceTransportGrantRejected
from app.services.infrastructure.browser_manager_client import (
    BrowserManagerClient,
)
from app.services.infrastructure.config_service import ConfigService
from tests.support.resource_owners.sessions import WORKSPACE_ID


@pytest.fixture
def browser_manager_client() -> BrowserManagerClient:
    return BrowserManagerClient(
        workspace_id=WORKSPACE_ID,
        backend_url="http://browser.test",
    )


class _JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_owner_client_sends_workspace_owner_and_per_call_actor_headers(
    browser_manager_client: BrowserManagerClient,
) -> None:
    with patch(
        "app.services.infrastructure.browser_manager_client.urlopen",
        return_value=_JsonResponse({"data": {"summary": "ok"}}),
    ) as mocked_urlopen:
        assert browser_manager_client._json_request_sync("GET", "/api/browsers/browser_1/read", None, "agent")[
            "data"
        ] == {"summary": "ok"}

    request = mocked_urlopen.call_args.args[0]
    assert request.get_header("X-boxteam-actor") == "agent"
    assert request.get_header("X-boxteam-workspace-id") == WORKSPACE_ID
    assert request.get_header("X-boxteam-resource-owner") == "workspace-browser"


def test_locked_error_is_reported_as_clean_tool_message(
    browser_manager_client: BrowserManagerClient,
) -> None:
    body = json.dumps(
        {
            "code": "browser_agent_access_locked",
            "error": "用户锁定了浏览器，你暂时不能操作这个页面: browser_id=browser_1",
        }
    ).encode("utf-8")
    http_error = HTTPError(
        "http://browser.test/api/browsers/browser_1/read",
        423,
        "Locked",
        {},
        BytesIO(body),
    )
    with (
        patch(
            "app.services.infrastructure.browser_manager_client.urlopen",
            side_effect=http_error,
        ),
        pytest.raises(RuntimeError, match="用户锁定了浏览器"),
    ):
        browser_manager_client._json_request_sync("GET", "/api/browsers/browser_1/read", None)


def test_timeout_error_preserves_retry_metadata(
    browser_manager_client: BrowserManagerClient,
) -> None:
    body = json.dumps(
        {
            "code": "browser_tool_timeout",
            "error": "Playwright 代码执行超时: 20000ms；浏览器页面已重置，可重试",
            "retryable": True,
            "recovery": "page_reset",
            "timeout_ms": 20_000,
        }
    ).encode("utf-8")
    http_error = HTTPError(
        "http://browser.test/api/browsers/browser_1/run",
        408,
        "Request Timeout",
        {},
        BytesIO(body),
    )
    with patch(
        "app.services.infrastructure.browser_manager_client.urlopen",
        side_effect=http_error,
    ), pytest.raises(BrowserRequestError, match="可重试") as error_info:
        browser_manager_client._json_request_sync("POST", "/api/browsers/browser_1/run", {})

    error = error_info.value
    assert error.status == 408
    assert error.code == "browser_tool_timeout"
    assert error.retryable is True
    assert error.recovery == "page_reset"


def test_playwright_request_timeout_covers_declared_browser_budget(
    browser_manager_client: BrowserManagerClient,
) -> None:
    with patch(
        "app.services.infrastructure.browser_manager_client.urlopen",
        return_value=_JsonResponse({"data": {"result": "ok"}}),
    ) as mocked_urlopen:
        result = browser_manager_client._json_request_sync(
            "POST",
            "/api/browsers/browser_1/run",
            {"code": "await page.waitForTimeout(53000);", "timeoutMs": 60000},
        )

    assert result == {"data": {"result": "ok"}}
    assert mocked_urlopen.call_args.kwargs["timeout"] == 70


def test_non_playwright_request_keeps_short_default_timeout(
    browser_manager_client: BrowserManagerClient,
) -> None:
    with patch(
        "app.services.infrastructure.browser_manager_client.urlopen",
        return_value=_JsonResponse({"data": {"summary": "ok"}}),
    ) as mocked_urlopen:
        browser_manager_client._json_request_sync("GET", "/api/browsers/browser_1/read", None)

    assert mocked_urlopen.call_args.kwargs["timeout"] == 30


def test_browser_backend_url_is_resolved_from_current_config_snapshot() -> None:
    config_service = Mock(spec=ConfigService)
    current_url = ["http://browser-a"]
    config_service.get_browser_backend_url.side_effect = lambda: current_url[0]
    client = BrowserManagerClient(
        workspace_id=WORKSPACE_ID,
        config_service=config_service,
    )
    with patch(
        "app.services.infrastructure.browser_manager_client.urlopen",
        return_value=_JsonResponse({"data": {"summary": "ok"}}),
    ) as mocked_urlopen:
        client._json_request_sync("GET", "/api/browsers", None)
        current_url[0] = "http://browser-b"
        client._json_request_sync("GET", "/api/browsers", None)

    assert [
        call.args[0].full_url for call in mocked_urlopen.call_args_list
    ] == [
        "http://browser-a/api/browsers",
        "http://browser-b/api/browsers",
    ]


@pytest.mark.asyncio
async def test_async_transport_timeout_becomes_retryable_browser_error(
    browser_manager_client: BrowserManagerClient,
) -> None:
    with patch.object(
        browser_manager_client,
        "_json_request_sync",
        side_effect=TimeoutError("timed out"),
    ), pytest.raises(
        BrowserRequestError, match="浏览器管理器操作超时",
    ) as error_info:
        await browser_manager_client.perform("browser_1", "read", {}, actor="agent")

    error = error_info.value
    assert error.status == 408
    assert error.code == "browser_tool_timeout"
    assert error.retryable is True
    assert error.recovery == "page_reset"
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
    browser_manager_client: BrowserManagerClient,
) -> None:
    body = json.dumps(grant_rejection_payload).encode()
    failure = HTTPError("http://browser.test/api/browsers/browser_test/transport-grant", 409, "Conflict", {}, BytesIO(body))
    with patch("app.services.infrastructure.browser_manager_client.urlopen", side_effect=failure), pytest.raises(ResourceTransportGrantRejected) as rejected:
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
    browser_manager_client: BrowserManagerClient,
) -> None:
    target = grant_rejection_payload if field in {"code", "operation_outcome", "error"} else grant_rejection_payload["browser"]
    target[field] = value
    failure = HTTPError("http://browser.test/api/browsers/browser_test/transport-grant", 409, "Conflict", {}, BytesIO(json.dumps(grant_rejection_payload).encode()))
    with patch("app.services.infrastructure.browser_manager_client.urlopen", side_effect=failure), pytest.raises((BrowserRequestError, ValueError, TypeError)):
        await browser_manager_client.issue_transport_grant("browser_test", owner_revision=2, association=None)
