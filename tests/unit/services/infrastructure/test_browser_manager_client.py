from __future__ import annotations

import json
from io import BytesIO
from typing import Self
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from app.services.infrastructure.browser_manager_client import (
    BrowserManagerClient,
    BrowserManagerRequestError,
)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_agent_client_sends_actor_header() -> None:
    client = BrowserManagerClient(backend_url="http://browser.test").for_actor(
        "agent"
    )
    with patch(
        "app.services.infrastructure.browser_manager_client.urlopen",
        return_value=_Response({"data": {"summary": "ok"}}),
    ) as mocked_urlopen:
        assert client._json_request_sync("GET", "/api/browsers/browser_1/read", None)[
            "data"
        ] == {"summary": "ok"}

    request = mocked_urlopen.call_args.args[0]
    assert request.get_header("X-boxteam-actor") == "agent"


def test_locked_error_is_reported_as_clean_tool_message() -> None:
    client = BrowserManagerClient(backend_url="http://browser.test").for_actor(
        "agent"
    )
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
        client._json_request_sync("GET", "/api/browsers/browser_1/read", None)


def test_timeout_error_preserves_retry_metadata() -> None:
    client = BrowserManagerClient(backend_url="http://browser.test").for_actor(
        "agent"
    )
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
    ), pytest.raises(BrowserManagerRequestError, match="可重试") as error_info:
        client._json_request_sync("POST", "/api/browsers/browser_1/run", {})

    error = error_info.value
    assert error.status == 408
    assert error.code == "browser_tool_timeout"
    assert error.retryable is True
    assert error.recovery == "page_reset"


def test_playwright_request_timeout_covers_declared_browser_budget() -> None:
    client = BrowserManagerClient(backend_url="http://browser.test")
    with patch(
        "app.services.infrastructure.browser_manager_client.urlopen",
        return_value=_Response({"data": {"result": "ok"}}),
    ) as mocked_urlopen:
        result = client._json_request_sync(
            "POST",
            "/api/browsers/browser_1/run",
            {"code": "await page.waitForTimeout(53000);", "timeoutMs": 60000},
        )

    assert result == {"data": {"result": "ok"}}
    assert mocked_urlopen.call_args.kwargs["timeout"] == 70


def test_non_playwright_request_keeps_short_default_timeout() -> None:
    client = BrowserManagerClient(backend_url="http://browser.test")
    with patch(
        "app.services.infrastructure.browser_manager_client.urlopen",
        return_value=_Response({"data": {"summary": "ok"}}),
    ) as mocked_urlopen:
        client._json_request_sync("GET", "/api/browsers/browser_1/read", None)

    assert mocked_urlopen.call_args.kwargs["timeout"] == 30


@pytest.mark.asyncio
async def test_async_transport_timeout_becomes_retryable_browser_error() -> None:
    client = BrowserManagerClient(backend_url="http://browser.test")
    with patch.object(
        client,
        "_json_request_sync",
        side_effect=TimeoutError("timed out"),
    ), pytest.raises(
        BrowserManagerRequestError,
        match="浏览器管理器操作超时",
    ) as error_info:
        await client.read_page("browser_1")

    error = error_info.value
    assert error.status == 408
    assert error.code == "browser_tool_timeout"
    assert error.retryable is True
    assert error.recovery == "page_reset"
    assert error.timeout_ms == 30_000
