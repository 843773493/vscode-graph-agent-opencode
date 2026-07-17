from __future__ import annotations

from collections.abc import Iterator

from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

from app.gateway.main import app, get_registry
from app.gateway.server.workspace_proxy import _proxy_headers


class _GatewayRegistryStub:
    active_workspace_id = "gw_test"


@pytest.fixture
def gateway_client() -> Iterator[TestClient]:
    app.dependency_overrides[get_registry] = lambda: _GatewayRegistryStub()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


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
