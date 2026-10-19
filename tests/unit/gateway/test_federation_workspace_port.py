"""Workspace HTTP 端口的 envelope 边界；真实进程请求由通信 Integration 覆盖。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.session_control_communication_ledger.observations import (
    observation_error_code,
)
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.gateway.federation.errors import (
    FEDERATION_DEADLINE_EXCEEDED,
    FEDERATION_SESSION_MAIN_UNAVAILABLE,
    FEDERATION_TARGET_NOT_RESOLVABLE,
    FEDERATION_TARGET_OWNER_REJECTED,
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
async def http_context(monkeypatch: pytest.MonkeyPatch):
    def response(request: httpx.Request) -> httpx.Response:
        context.requests.append(request)
        headers = {"X-Request-ID": context.header_request_id}
        if context.raw is not None:
            return httpx.Response(context.status, content=context.raw, headers=headers)
        return httpx.Response(context.status, json=context.data, headers=headers)

    original = httpx.AsyncClient
    transport = httpx.MockTransport(response)
    client = original(transport=transport, trust_env=False)
    monkeypatch.setattr(
        "app.gateway.federation.workspace_port.httpx.AsyncClient",
        lambda **kwargs: original(transport=transport, **kwargs),
    )
    port, catalog = WorkspaceSessionMainPort(http_client=client), WorkspaceCatalogPort()
    for owner in (port, catalog):
        owner.project_workspaces(
            (("gateway-route", "http://workspace.test", WORKSPACE_ID),)
        )
    context = _HTTPContext(port, catalog)
    monkeypatch.setattr(
        "app.gateway.federation.workspace_port.load_proxy_gateway_id",
        lambda: "gateway_local",
    )
    token = set_current_request_id(REQUEST_ID)
    gateway_token = set_current_gateway_id("gateway_local")
    try:
        yield context
    finally:
        reset_current_gateway_id(gateway_token)
        reset_current_request_id(token)
        await client.aclose()


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
    assert request.extensions["timeout"] == {
        "connect": 5, "read": 5, "write": 5, "pool": 5,
    }
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


@pytest.mark.asyncio
async def test_main_http_pool_keeps_concurrent_identity_and_current_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    both_entered = asyncio.Event()

    async def response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 2:
            both_entered.set()
        await both_entered.wait()
        request_id = request.headers["X-Request-ID"]
        return httpx.Response(
            200, headers={"X-Request-ID": request_id},
            json={"request_id": request_id, "data": {
                "session_id": TARGET_SESSION, "thread_id": TARGET_THREAD,
            }},
        )

    monkeypatch.setattr(
        "app.gateway.federation.workspace_port.load_proxy_gateway_id",
        lambda: "gateway_local",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False) as client:
        port = WorkspaceSessionMainPort(http_client=client, request_timeout=3)
        port.project_workspaces((
            ("route-one", "http://workspace-one.test", "workspace_one"),
            ("route-two", "http://workspace-two.test", "workspace_two"),
        ))

        async def resolve(workspace_id: str, request_id: str, origin: str) -> str:
            token = set_current_request_id(request_id)
            try:
                return await port.resolve_main_thread(
                    gateway_id=origin, workspace_id=workspace_id, session_id=TARGET_SESSION,
                )
            finally:
                reset_current_request_id(token)

        results = await asyncio.gather(
            resolve("workspace_one", "req_pool_one", "gateway_one"),
            resolve("workspace_two", "req_pool_two", "gateway_two"),
        )
        assert results == [TARGET_THREAD, TARGET_THREAD]
        assert not client.is_closed
        assert [(request.url.host, request.headers["X-BoxTeam-Workspace-Id"],
                 request.headers["X-Request-ID"], request.headers["X-BoxTeam-Communication-Origin"])
                for request in requests] == [
            ("workspace-one.test", "workspace_one", "req_pool_one", "gateway_one"),
            ("workspace-two.test", "workspace_two", "req_pool_two", "gateway_two"),
        ]
        assert all(request.extensions["timeout"] == {
            "connect": 3, "read": 3, "write": 3, "pool": 3,
        } for request in requests)
        port.project_workspaces((("route-one", "http://workspace-new.test", "workspace_one"),))
        assert await resolve("workspace_one", "req_pool_new", "gateway_new") == TARGET_THREAD
        assert requests[-1].url.host == "workspace-new.test"
        assert requests[-1].headers["X-Request-ID"] == "req_pool_new"
        assert requests[-1].headers["X-BoxTeam-Communication-Origin"] == "gateway_new"


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


@pytest.fixture
def context_read_body() -> dict[str, object]:
    uri = session_resource_uri(WORKSPACE_ID, TARGET_SESSION, gateway_authority="gateway_local")
    target = {
        "gateway_id": "gateway_local", "resource_identity": str(session_resource_identity(WORKSPACE_ID, TARGET_SESSION)),
        "uri": uri, "scope": "workspace",
    }
    return {
        "source_gateway_id": "gateway_source", "resource_target": target,
        "read": {key: target[key] for key in ("resource_identity", "uri", "scope")} | {"view": "information"},
    }


@pytest.mark.parametrize(
    ("status", "detail", "expected_code", "expected_owner_code", "expected_owner"),
    [
        (404, "missing", FEDERATION_TARGET_NOT_RESOLVABLE, None, None),
        (403, "unshared", FEDERATION_TARGET_NOT_RESOLVABLE, None, None),
        (400, {"code": "unsupported_view"}, FEDERATION_TARGET_OWNER_REJECTED, "unsupported_view", "resource_uri_resolver"),
        (400, {"code": "scope_mismatch"}, FEDERATION_TARGET_OWNER_REJECTED, "scope_mismatch", "resource_uri_resolver"),
        (409, {"code": "snapshot_changed"}, FEDERATION_TARGET_OWNER_REJECTED, "snapshot_changed", "context_revision"),
        (400, {"code": "made-up-code", "message": "/private/path"}, FEDERATION_SESSION_MAIN_UNAVAILABLE, None, None),
    ],
)
async def test_context_query_preserves_owner_reject_codes_without_locator_disclosure(
    http_context: _HTTPContext, context_read_body: dict[str, object],
    status: int, detail: object, expected_code: str,
    expected_owner_code: str | None, expected_owner: str | None,
) -> None:
    http_context.status = status
    http_context.data = {"request_id": REQUEST_ID, "detail": detail}
    with pytest.raises(FederationError) as error:
        await http_context.port.execute(
            operation="read", target=context_read_body["resource_target"], body=context_read_body,
            origin="gateway_source", timeout=5,
        )
    assert error.value.code == expected_code
    assert "/private/path" not in str(error.value)
    assert error.value.detail.get("code") == expected_owner_code
    assert error.value.detail.get("owner") == expected_owner
    assert len(http_context.requests) == 1
    assert http_context.requests[0].url.path == "/api/v1/context/read"


async def test_observation_rejection_keeps_owner_code_for_existing_consumer(
    http_context: _HTTPContext, read_body: dict[str, object],
) -> None:
    http_context.status = 409
    http_context.data = {
        "request_id": REQUEST_ID,
        "detail": {
            "code": "read-snapshot-invalid",
            "message": "/private/path",
            "detail": {"locator": "/private/path"},
        },
    }

    with pytest.raises(FederationError) as error:
        await http_context.port.execute(
            operation="read",
            target=read_body["target"],
            body=read_body,
            origin="gateway_source",
            timeout=5,
        )

    assert error.value.code == FEDERATION_TARGET_OWNER_REJECTED
    assert error.value.detail == {
        "owner": "communication_observation",
        "code": "read-snapshot-invalid",
    }
    assert observation_error_code({"detail": error.value.to_wire()}) == (
        "read-snapshot-invalid"
    )
    assert "/private/path" not in str(error.value)
    assert "/private/path" not in repr(error.value.to_wire())
    assert http_context.requests[0].url.path == (
        f"/api/v1/sessions/{TARGET_SESSION}/communication-observations"
    )


async def test_context_target_rejects_unregistered_workspace_before_http(
    http_context: _HTTPContext, context_read_body: dict[str, object],
) -> None:
    http_context.port.project_workspaces(())
    with pytest.raises(FederationError) as error:
        await http_context.port.execute(
            operation="read", target=context_read_body["resource_target"], body=context_read_body,
            origin="gateway_source", timeout=5,
        )
    assert error.value.code == FEDERATION_TARGET_NOT_RESOLVABLE
    assert not http_context.requests


async def test_context_target_enforces_one_total_deadline(
    http_context: _HTTPContext, context_read_body: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def wait_beyond_deadline(*_args, **_kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(http_context.port, "read_resource_context", wait_beyond_deadline)
    with pytest.raises(FederationError) as error:
        await http_context.port.execute(
            operation="read", target=context_read_body["resource_target"], body=context_read_body,
            origin="gateway_source", timeout=0.02,
        )
    assert error.value.code == FEDERATION_DEADLINE_EXCEEDED
    assert not http_context.requests
