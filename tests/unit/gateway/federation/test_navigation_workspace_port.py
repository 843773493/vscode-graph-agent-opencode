"""只读导航到 Workspace owner 的真实 HTTP envelope 与拒绝边界。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.gateway.federation.errors import FederationError
from tests.support.communication import WORKSPACE_ID
from tests.unit.gateway.test_federation_workspace_port import _HTTPContext
from tests.unit.gateway.test_federation_workspace_port import (
    http_context as http_context,  # noqa: PLC0414 - 复用真实 HTTP port fixture
)

GATEWAY_ID = "gateway_3097a8378d6e2c98a52a687e4a004769"
URI = f"boxteam://{GATEWAY_ID}/workspace/{WORKSPACE_ID}/resources/files/root"
TARGET = {"gateway_id": GATEWAY_ID, "uri": URI}
BODY = {"source_gateway_id": "gateway_source", "navigation_target": TARGET, "navigation": {"uri": URI, "operation": "list"}}


async def test_navigation_calls_fixed_workspace_owner_with_remaining_budget(http_context: _HTTPContext) -> None:
    normalized = f"boxteam://workspace/{WORKSPACE_ID}/resources/files/root"
    http_context.data["data"] = {"node": {"uri": normalized, "resource_shape": "directory", "operations": ["describe", "list"], "child_path": "file_segments"}}
    result = await http_context.port.execute(operation="read", target=TARGET, body=BODY, origin="gateway_source", timeout=2.5)
    assert result["node"]["uri"] == normalized
    request = http_context.requests[0]
    assert request.url.path == "/api/v1/resources/navigation"
    assert request.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
    assert request.headers["X-BoxTeam-Communication-Origin"] == "gateway_source"
    assert json.loads(request.content)["uri"] == normalized
    assert set(request.extensions["timeout"].values()) == {2.5}


async def test_navigation_invalid_owner_reply_never_discloses_raw_data(http_context: _HTTPContext) -> None:
    http_context.data["data"] = {"node": {"uri": "/private/owner-locator", "resource_shape": "/private/owner-locator"}}
    with pytest.raises(FederationError) as error:
        await http_context.port.execute(operation="read", target=TARGET, body=BODY, origin="gateway_source", timeout=2.5)
    assert error.value.code == "federation-malformed-frame"
    assert "/private/owner-locator" not in str(error.value)


@pytest.mark.parametrize("failure,code", [
    (httpx.ConnectError, "federation-session-main-unavailable"),
    (httpx.ReadTimeout, "federation-deadline-exceeded"),
])
async def test_navigation_transport_errors_do_not_expose_provider_locator(http_context: _HTTPContext, monkeypatch: pytest.MonkeyPatch, failure: type[httpx.RequestError], code: str) -> None:
    async def fail(*_args, **_kwargs):
        raise failure("http://private-owner-locator:9999/api/v1/resources/navigation")
    monkeypatch.setattr(http_context.port._http_client, "request", fail)
    with pytest.raises(FederationError) as error:
        await http_context.port.execute(operation="read", target=TARGET, body=BODY, origin="gateway_source", timeout=2.5)
    assert error.value.code == code
    assert "private-owner-locator" not in str(error.value)
