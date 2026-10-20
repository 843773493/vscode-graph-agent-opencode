"""只读导航到 Workspace owner 的真实 HTTP envelope 与拒绝边界。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
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


@pytest.fixture(autouse=True)
async def navigation_gateway_context(http_context):
    # 通用 port fixture 的占位 Gateway 不属于此处冻结的导航 audience。
    token = set_current_gateway_id(GATEWAY_ID)
    try:
        yield
    finally:
        reset_current_gateway_id(token)


@pytest.mark.parametrize("available", [True, False])
async def test_native_navigation_uses_local_executor_and_preserves_gateway_root(http_context: _HTTPContext, available: bool) -> None:
    native = f"boxteam://gateway/{GATEWAY_ID}/resources/files/01900000000070008000000000000001"
    qualified = native.replace("boxteam://", f"boxteam://{GATEWAY_ID}/")
    target = {"gateway_id": GATEWAY_ID, "uri": qualified}
    body = {"source_gateway_id": "gateway_source", "navigation_target": target,
            "navigation": {"uri": qualified, "operation": "list"}}
    executor_id = "00000000-0000-4000-8000-000000000001"
    http_context.port.project_workspaces((("later", "http://later.test", WORKSPACE_ID),
                                           ("executor", "http://native.test", executor_id)) if available else ())
    http_context.data["data"] = {"node": {"uri": native, "resource_shape": "directory", "operations": ["describe", "list"], "child_path": "file_segments"}}
    if not available:
        with pytest.raises(FederationError) as error:
            await http_context.port.execute(operation="read", target=target, body=body, origin="gateway_source", timeout=2.5)
        assert error.value.code == "target_not_resolvable"
        assert http_context.requests == []
        return
    result = await http_context.port.execute(operation="read", target=target, body=body, origin="gateway_source", timeout=2.5)
    assert result["node"]["uri"] == native
    request, = http_context.requests
    assert request.url.host == "native.test"
    assert request.headers["X-BoxTeam-Workspace-Id"] == executor_id
    assert json.loads(request.content)["uri"] == native


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
