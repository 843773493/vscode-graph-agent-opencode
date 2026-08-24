from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.history_loading import HistoryLoadingConfig
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
