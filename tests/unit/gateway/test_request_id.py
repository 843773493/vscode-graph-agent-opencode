from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.history_loading import HistoryLoadingConfig
from app.gateway.auxiliary_proxy import (
    _proxy_request_headers as _auxiliary_proxy_headers,
)
from app.gateway.config import GatewayConfig
from app.gateway.control.scheduler import SessionGeneratorScheduler
from app.gateway.main import app, get_registry
from app.gateway.server.workspace_proxy import _proxy_headers


class _GatewayRegistryStub:
    active_workspace_id = "gw_test"


@pytest.fixture
def gateway_client() -> Iterator[TestClient]:
    app.dependency_overrides[get_registry] = lambda: _GatewayRegistryStub()
    app.state.session_generator_scheduler = MagicMock(
        spec=SessionGeneratorScheduler
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        del app.state.session_generator_scheduler


def test_gateway_endpoint_returns_middleware_request_id(
    gateway_client: TestClient,
) -> None:
    response = gateway_client.get(
        "/api/gateway/health",
        headers={"X-Request-ID": "req_gateway_test"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_gateway_test"
    assert response.json()["request_id"] == "req_gateway_test"
    assert response.json()["data"]["process_id"] == os.getpid()
    assert response.json()["data"]["development_restart_available"] is False


def test_gateway_auth_error_contains_request_id_in_body(
    gateway_client: TestClient,
) -> None:
    response = gateway_client.get(
        "/api/v1/workspace",
        headers={"X-Request-ID": "req_gateway_auth_error"},
    )

    assert response.status_code == 401
    assert response.headers["X-Request-ID"] == "req_gateway_auth_error"
    assert response.json() == {
        "detail": "缺少 Gateway 访问凭据",
        "request_id": "req_gateway_auth_error",
    }


def test_gateway_proxy_forwards_authoritative_request_id() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/workspace",
            "headers": [(b"x-request-id", b"untrusted_duplicate")],
        }
    )
    request.state.request_id = "req_gateway_proxy"

    headers = _proxy_headers(request)

    assert headers["X-Request-ID"] == "req_gateway_proxy"
    assert headers["X-Local-Token"] == "local-dev-token"


def _upstream_request_id_values(headers: dict[str, str]) -> list[str]:
    """把代理计算出的头部真正建成上游请求，读出线上可见的全部 request id。"""

    forwarded = httpx.Client().build_request(
        "GET",
        "http://upstream.test/api/v1/workspace",
        headers=headers,
    )
    return [
        value.decode()
        for key, value in forwarded.headers.raw
        if key.lower() == b"x-request-id"
    ]


@pytest.mark.parametrize(
    "build_headers",
    [
        pytest.param(_proxy_headers, id="workspace-api"),
        pytest.param(_auxiliary_proxy_headers, id="auxiliary-service"),
    ],
)
def test_gateway_proxy_does_not_append_second_request_id_on_duplicate_inbound(
    build_headers,
) -> None:
    """客户端重复发送 X-Request-ID 时，代理不得让第二个请求 ID 到达上游。

    AGENTS.md 要求「任何一层不得补造第二个请求 ID」。原实现按大小写敏感的键名
    过滤入站头部（丢弃 ``x-request-id`` 却保留 ``X-Request-ID``），随后再写入
    Gateway 的权威值，于是上游同时收到客户端伪造值与权威值两个头部；上游按
    ``.get()`` 取到的是客户端那个，响应体与 X-Request-ID 头因此身份不一致。
    """

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/workspace",
            "headers": [
                (b"x-request-id", b"req_first_from_client"),
                (b"X-Request-ID", b"req_second_from_client"),
            ],
        }
    )
    request.state.request_id = "req_authoritative"

    values = _upstream_request_id_values(build_headers(request))

    assert values == ["req_authoritative"]


def test_gateway_proxy_overwrites_inbound_history_policy() -> None:
    application = FastAPI()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/sessions/session/history",
            "app": application,
            "headers": [
                (b"x-request-id", b"request"),
                (b"x-boxteam-history-loading", b"{\"anchor_before_turns\":999}"),
            ],
        }
    )
    request.state.request_id = "req_history_policy"
    application.state.gateway_config = GatewayConfig(
        history_loading=HistoryLoadingConfig(
            anchor_before_turns=4,
            anchor_after_turns=4,
        )
    )

    headers = _proxy_headers(request)

    assert "999" not in headers["X-BoxTeam-History-Loading"]
    assert '"anchor_before_turns":4' in headers["X-BoxTeam-History-Loading"]
    assert '"anchor_after_turns":4' in headers["X-BoxTeam-History-Loading"]
