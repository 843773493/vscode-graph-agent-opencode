"""Workspace HTTP 端口的 envelope 边界；真实进程请求由通信 Integration 覆盖。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.trace_middleware import reset_current_request_id, set_current_request_id
from app.gateway.federation.errors import (
    FEDERATION_SESSION_MAIN_UNAVAILABLE,
    FederationError,
)
from app.gateway.federation.workspace_port import (
    WorkspaceCatalogPort,
    WorkspaceSessionMainPort,
)
from app.services.business.session_context_resource import (
    session_resource_identity,
    session_resource_uri,
)
from tests.support.communication import (
    SOURCE_SESSION,
    SOURCE_THREAD,
    TARGET_SESSION,
    TARGET_THREAD,
    WORKSPACE_ID,
)

REQUEST_ID = "req_workspace_http_port"


@dataclass
class _HTTPContext:
    port: WorkspaceSessionMainPort
    catalog: WorkspaceCatalogPort
    data: object = field(
        default_factory=lambda: {
            "request_id": REQUEST_ID,
            "data": {"session_id": TARGET_SESSION, "thread_id": TARGET_THREAD},
        }
    )
    requests: list[httpx.Request] = field(default_factory=list)
    header_request_id: str = REQUEST_ID
    status: int = 200
    raw: bytes | None = None


@pytest.fixture
def http_context(monkeypatch: pytest.MonkeyPatch):
    port, catalog = WorkspaceSessionMainPort(), WorkspaceCatalogPort()
    for owner in (port, catalog):
        owner.project_workspaces(
            (("gateway-route", "http://workspace.test", WORKSPACE_ID),)
        )
    context = _HTTPContext(port, catalog)

    def response(request: httpx.Request) -> httpx.Response:
        context.requests.append(request)
        headers = {"X-Request-ID": context.header_request_id}
        if context.raw is not None:
            return httpx.Response(context.status, content=context.raw, headers=headers)
        return httpx.Response(context.status, json=context.data, headers=headers)

    original = httpx.AsyncClient
    transport = httpx.MockTransport(response)
    monkeypatch.setattr(
        "app.gateway.federation.workspace_port.httpx.AsyncClient",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    monkeypatch.setattr(
        "app.gateway.federation.workspace_port.load_proxy_gateway_id",
        lambda: "gateway_local",
    )
    token = set_current_request_id(REQUEST_ID)
    yield context
    reset_current_request_id(token)


@pytest.fixture
def read_body() -> dict[str, object]:
    return {
        "observation_id": "obs_http",
        "source_call_id": "cop_http",
        "page_identity": "page_http",
        "page_ordinal": 0,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "operation": "read",
        "source": {
            "gateway_id": "gateway_source",
            "workspace_id": "11111111-1111-4111-8111-111111111111",
            "session_id": SOURCE_SESSION,
            "thread_id": SOURCE_THREAD,
        },
        "target": {
            "gateway_id": "gateway_local",
            "workspace_id": WORKSPACE_ID,
            "session_id": TARGET_SESSION,
            "thread_id": TARGET_THREAD,
        },
        "query": {
            "read": {
                "resource_identity": str(
                    session_resource_identity(WORKSPACE_ID, TARGET_SESSION)
                ),
                "uri": session_resource_uri(WORKSPACE_ID, TARGET_SESSION),
                "scope": "workspace",
                "view": "messages",
            },
        },
    }


async def test_main_http_uses_backend_uuid_and_preserves_request_identity(
    http_context: _HTTPContext,
) -> None:
    main = await http_context.port.resolve_main_thread(
        gateway_id="gateway_local", workspace_id=WORKSPACE_ID, session_id=TARGET_SESSION
    )
    assert main == TARGET_THREAD
    request = http_context.requests[0]
    assert request.url.path == f"/api/v1/sessions/{TARGET_SESSION}/main-thread"
    assert request.headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID
    assert request.headers["X-BoxTeam-Gateway-Id"] == "gateway_local"
    assert request.headers["X-Request-ID"] == REQUEST_ID
    assert http_context.port.canonical_workspace_id("gateway-route") == WORKSPACE_ID


@pytest.mark.parametrize(
    "bad",
    [
        "non_json",
        "header_id",
        "body_id",
        "missing_data",
        "session_mismatch",
        "http_error",
    ],
)
async def test_main_http_rejects_untrusted_response(
    http_context: _HTTPContext, bad: str
) -> None:
    if bad == "non_json":
        http_context.raw = b"upstream is not JSON"
    elif bad == "header_id":
        http_context.header_request_id = "req_different"
    elif bad == "body_id":
        http_context.data["request_id"] = "req_different"
    elif bad == "missing_data":
        http_context.data = {"request_id": REQUEST_ID}
    elif bad == "session_mismatch":
        http_context.data["data"]["session_id"] = SOURCE_SESSION
    else:
        http_context.status = 503
    with pytest.raises(FederationError) as error:
        await http_context.port.resolve_main_thread(
            gateway_id="gateway_local",
            workspace_id=WORKSPACE_ID,
            session_id=TARGET_SESSION,
        )
    assert error.value.code == FEDERATION_SESSION_MAIN_UNAVAILABLE
    assert len(http_context.requests) == 1


async def test_business_relay_keeps_source_and_body(
    http_context: _HTTPContext, read_body: dict[str, object]
) -> None:
    http_context.data = {
        "request_id": REQUEST_ID,
        "data": {"observation_id": "obs_http", "items": []},
    }
    result = await http_context.port.execute(
        operation="read",
        target=read_body["target"],
        body=read_body,
        origin="gateway_source",
        timeout=5,
    )
    assert result == {"observation_id": "obs_http", "items": []}
    request = http_context.requests[0]
    assert (
        request.url.path
        == f"/api/v1/sessions/{TARGET_SESSION}/communication-observations"
    )
    assert request.headers["X-BoxTeam-Communication-Origin"] == "gateway_source"
    assert request.headers["X-Request-ID"] == REQUEST_ID
    import json

    assert json.loads(request.content) == read_body


@pytest.mark.parametrize("change", ["origin", "target", "operation"])
async def test_business_relay_rejects_identity_drift_before_http(
    http_context: _HTTPContext, read_body: dict[str, object], change: str
) -> None:
    target, origin, operation = read_body["target"], "gateway_source", "read"
    if change == "origin":
        origin = "gateway_forged"
    elif change == "target":
        target = {**target, "thread_id": SOURCE_THREAD}
    else:
        operation = "wait"
    with pytest.raises(FederationError):
        await http_context.port.execute(
            operation=operation, target=target, body=read_body, origin=origin, timeout=5
        )
    assert http_context.requests == []


async def test_cold_catalog_returns_backend_identity(
    http_context: _HTTPContext,
) -> None:
    http_context.data = {
        "request_id": REQUEST_ID,
        "data": {"items": [{"session_id": TARGET_SESSION}]},
    }
    assert await http_context.catalog.local_session_workspaces(TARGET_SESSION) == (
        WORKSPACE_ID,
    )
    assert http_context.requests[0].headers["X-BoxTeam-Workspace-Id"] == WORKSPACE_ID


async def test_cold_catalog_rejects_non_json(http_context: _HTTPContext) -> None:
    http_context.raw = b"not JSON"
    with pytest.raises(FederationError, match="不是 JSON") as error:
        await http_context.catalog.local_session_workspaces(TARGET_SESSION)
    assert error.value.code == FEDERATION_SESSION_MAIN_UNAVAILABLE
