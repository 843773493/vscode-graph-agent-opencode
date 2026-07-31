from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from app.services.infrastructure.browser_manager_client import BrowserManagerClient


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
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
