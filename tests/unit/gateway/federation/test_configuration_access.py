"""生产 ASGI 配置准入与流披露；下游 HTTP 使用受控 transport，不启动真实服务。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.config import GatewayConfigReloadService, load_gateway_config
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.federation.policy import Capability, FederationPolicyStore
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.main import gateway_http_exception_handler
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes.health_config import router as config_router
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.workspace_proxy import router as proxy_router
from app.services.infrastructure.config.state import ConfigEventInput

WORKSPACE_ID = "00000000-0000-4000-8000-000000000001"
PEER = "gateway_" + "a" * 32


@dataclass
class Backend:
    requests: list[httpx.Request] = field(default_factory=list)
    stream: httpx.AsyncByteStream | None = None
    before_response: Callable[[], None] | None = None

    async def respond(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        if self.before_response is not None:
            self.before_response()
        if self.stream is not None:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.stream)
        return httpx.Response(200, json={"data": {"source_key": "native-source"}})


@dataclass
class ConfigurationAccess:
    app: FastAPI
    client: httpx.AsyncClient
    registry: GatewayWorkspaceRegistry
    policy: FederationPolicyStore
    backend: Backend
    service: GatewayConfigReloadService
    state: GatewayStateStore
    token: str
    gateway_id: str

    def allow(self, *capabilities: Capability, principal: str | None = None) -> None:
        self.policy.publish({"rules": [
            {"capability": capability, "effect": "allow", "principal_ref": principal or principal_ref_for_gateway(PEER)}
            for capability in capabilities
        ]})


@pytest.fixture
async def configuration_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ConfigurationAccess]:
    home = tmp_path / "home"
    root = home / "gateway"
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(root))
    gateway_id = load_or_create_gateway_id(root / "identity.json")
    token = FederationCredentialStore(storage_path=root / "credentials/federation.json").issue(
        connection_id="configuration-peer", peer_gateway_id=PEER,
    ).token
    config_path = home / "config/gateway.jsonc"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"config_version": 4, "workspaces": []}))
    schema_path = Path.cwd() / "configs/gateway_schema.jsonc"
    state = GatewayStateStore(path=root / "gateway.sqlite")
    config = load_gateway_config(config_path=config_path, schema_path=schema_path, state_store=state)
    service = GatewayConfigReloadService(
        state_store=state, config=config, config_path=config_path,
        local_config_path=config_path.with_name("gateway_local.jsonc"), schema_path=schema_path, gateway_id=gateway_id,
    )
    service.initialize_active_snapshot()
    registry = GatewayWorkspaceRegistry(gateway_id=gateway_id, storage_path=root / "registry.json")
    registry.upsert(WorkspaceTarget(
        gateway_id=gateway_id, workspace_id=WORKSPACE_ID, name="原生工作区", root_path=str(tmp_path / "workspace"),
        backend_url="http://backend.test", connection_kind="local",
    ), runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://backend.test"}))
    policy = FederationPolicyStore()
    backend = Backend()
    app = FastAPI()
    app.add_middleware(TraceMiddleware)

    async def http_error(request: Request, error: Exception) -> Response:
        assert isinstance(error, HTTPException)
        return await gateway_http_exception_handler(request, error)

    app.add_exception_handler(HTTPException, http_error)
    app.include_router(config_router)
    app.include_router(proxy_router)
    app.state.registry = registry
    app.state.gateway_id = gateway_id
    app.state.gateway_state = state
    app.state.gateway_config_reload = service
    app.state.federation_policy_store = policy
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(backend.respond)) as upstream:
            app.state.http_client = app.state.streaming_http_client = upstream
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway.test",
                headers={"X-BoxTeam-Federation-Token": token, "X-Request-ID": "configuration-access"},
            ) as client:
                yield ConfigurationAccess(app, client, registry, policy, backend, service, state, token, gateway_id)
    finally:
        registry.close()
        state.close()


@pytest.mark.parametrize("capability", ["read", "discovery", "managed_workspace_admin", "configuration_write"])
@pytest.mark.parametrize("path", ["config/sources/native", f"workspaces/{WORKSPACE_ID}/config/sources/native"])
async def test_other_capabilities_never_authorize_configuration_read(
    configuration_access: ConfigurationAccess, capability: Capability, path: str,
) -> None:
    case = configuration_access
    case.allow(capability)
    response = await case.client.get(f"/api/v1/{path}", headers={"X-BoxTeam-Gateway-Id": "forged-principal"})
    assert response.status_code == 403, response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == "configuration-access"
    assert not case.backend.requests
    assert case.registry.route_reference_counts(WORKSPACE_ID) == (0, 0)


@pytest.mark.parametrize("binding", [{}, {"origin_gateway_id": PEER}, {"principal_ref": principal_ref_for_gateway("other")}])
async def test_wildcard_and_wrong_principal_cannot_read_config(configuration_access: ConfigurationAccess, binding: dict[str, str]) -> None:
    case = configuration_access
    case.policy.publish({"rules": [{"capability": "configuration_read", "effect": "allow", **binding}]})
    response = await case.client.get("/api/v1/config/reload-status")
    assert response.status_code == 403 and not case.backend.requests


@pytest.mark.parametrize("method,path", [
    ("POST", "sources/native/preflight"), ("PUT", "sources/native"), ("PATCH", ""),
    ("POST", "pending/retry"), ("POST", "pending/discard"), ("POST", "pending/resolve"),
    ("POST", "pending/restart-failed"),
])
@pytest.mark.parametrize("capability", ["configuration_read", "configuration_write"])
async def test_configuration_mutations_require_write_and_projected_read_before_forwarding(
    configuration_access: ConfigurationAccess, method: str, path: str, capability: Capability,
) -> None:
    case = configuration_access
    case.allow(capability)
    response = await case.client.request(method, f"/api/v1/workspaces/{WORKSPACE_ID}/config/{path}", json={})
    assert response.status_code == 403, response.text
    assert not case.backend.requests


async def test_exact_read_write_reaches_original_backend_and_revocation_stops_next_write(configuration_access: ConfigurationAccess) -> None:
    case = configuration_access
    case.allow("configuration_read", "configuration_write")
    path = f"/api/v1/workspaces/{WORKSPACE_ID}/config/sources/native"
    response = await case.client.put(path, json={"idempotency_key": "same-intent"})
    assert response.status_code == 200 and response.json()["data"]["source_key"] == "native-source"
    forwarded, = case.backend.requests
    assert forwarded.url.host == "backend.test" and forwarded.url.path == path
    assert forwarded.headers["X-BoxTeam-Gateway-Id"] == case.gateway_id
    assert not forwarded.headers.get("X-BoxTeam-Federation-Token")
    assert json.loads(forwarded.content) == {"idempotency_key": "same-intent"}
    case.allow("configuration_read")
    denied = await case.client.put(path, json={"idempotency_key": "next-intent"})
    assert denied.status_code == 403 and len(case.backend.requests) == 1


async def test_read_revoked_while_waiting_for_upstream_cannot_disclose_response(configuration_access: ConfigurationAccess) -> None:
    case = configuration_access
    case.allow("configuration_read")

    def revoke() -> None:
        case.policy.publish({"rules": []})

    case.backend.before_response = revoke
    response = await case.client.get("/api/v1/config/sources/native")
    assert response.status_code == 403 and "native-source" not in response.text
    assert len(case.backend.requests) == 1 and case.registry.route_reference_counts(WORKSPACE_ID) == (0, 0)


@pytest.mark.parametrize("path,status", [
    ("config/sources/native", 403), ("%2563onfig/sources/native", 403),
    (f"workspaces/{WORKSPACE_ID}/anything/../config/sources/native", 403),
    (f"workspaces/{WORKSPACE_ID}/anything/%2e%2e/config/sources/native", 400),
    (f"workspaces/{WORKSPACE_ID}/anything/%252e%252e/config/sources/native", 400),
])
async def test_encoded_configuration_and_dot_paths_cannot_enter_backend(
    configuration_access: ConfigurationAccess, path: str, status: int,
) -> None:
    case = configuration_access
    response = await case.client.get(f"/api/v1/{path}")
    assert response.status_code == status, response.text
    assert not case.backend.requests and case.registry.route_reference_counts(WORKSPACE_ID) == (0, 0)


@pytest.mark.parametrize("path", ["sources", "reload-status", "events", "events/stream"])
async def test_gateway_configuration_gets_require_configuration_read(configuration_access: ConfigurationAccess, path: str) -> None:
    response = await configuration_access.client.get(f"/api/gateway/config/{path}")
    assert response.status_code == 403, response.text


@pytest.mark.parametrize("path,body", [
    ("retry-restart", None),
    ("discard-restart", {"expected_active_revision": 1, "expected_active_digest": "digest"}),
    ("resolve-restart", {"candidate_ref": "candidate", "candidate_id": "candidate-id", "generation": "generation",
                         "loaded_source": "pending", "pending_revision": 2, "candidate_digest": "candidate-digest",
                         "effective_digest": "effective-digest", "secret_binding_digest": "binding-digest",
                         "fencing_token_digest": "fencing-digest", "health_digest": "health-digest", "gateway_id": "gateway"}),
])
@pytest.mark.parametrize("capability", ["configuration_read", "configuration_write"])
async def test_gateway_pending_mutations_require_both_capabilities_before_owner(
    configuration_access: ConfigurationAccess, monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, object] | None,
    capability: Capability,
) -> None:
    case = configuration_access
    case.allow(capability)
    calls: list[str] = []
    method = {"retry-restart": "retry_pending_restart", "discard-restart": "discard_pending_restart", "resolve-restart": "resolve_pending_restart"}[path]

    def accepted(**_: object):
        calls.append(method)
        return case.service.status()

    monkeypatch.setattr(case.service, method, accepted)
    denied = await case.client.post(f"/api/gateway/config/{path}?candidate_ref=candidate", json=body)
    assert denied.status_code == 403 and not calls
    case.allow("configuration_read", "configuration_write")
    allowed = await case.client.post(f"/api/gateway/config/{path}?candidate_ref=candidate", json=body)
    assert allowed.status_code == 200, allowed.text
    assert calls == [method]


async def test_local_config_access_keeps_original_authentication(configuration_access: ConfigurationAccess) -> None:
    case = configuration_access
    response = await case.client.get("/api/gateway/config/reload-status", headers={"X-Local-Token": get_gateway_local_token()})
    assert response.status_code == 200, response.text
    case.allow("configuration_read", "configuration_write", "managed_workspace_admin")
    denied = await case.client.post("/api/gateway/runtime/restart-development")
    assert denied.status_code == 403 and "远程 Gateway" in denied.json()["detail"]


class ConfigurationStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index in (1, 2):
            yield f'event: config\ndata: {{"event_seq":{index},"source":"private-{index}"}}\n\n'.encode()

    async def aclose(self) -> None:
        self.closed = True


async def _consume_sse_with_revocation(case: ConfigurationAccess, path: str) -> bytes:
    frames: list[bytes] = []
    delivered_request = False

    async def receive() -> Message:
        nonlocal delivered_request
        if not delivered_request:
            delivered_request = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        raise AssertionError("测试只由生产流关闭结束")

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            frame = message.get("body", b"")
            frames.append(frame)
            if b"event: config\n" in frame:
                case.policy.publish({"rules": []})

    await asyncio.wait_for(case.app({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(b"x-boxteam-federation-token", case.token.encode())], "server": ("gateway.test", 80), "client": ("unit", 1),
    }, receive, send), timeout=3)
    return b"".join(frames)


@pytest.mark.parametrize("gateway_owned", [False, True])
async def test_production_configuration_sse_revocation_emits_public_error_and_stops_disclosure(
    configuration_access: ConfigurationAccess, gateway_owned: bool,
) -> None:
    case = configuration_access
    case.allow("configuration_read")
    stream = ConfigurationStream()
    case.backend.stream = stream
    for index in (1, 2):
        case.state.append_config_event(ConfigEventInput(
            event_id=f"config-access-{index}", config_domain="gateway", candidate_id=None, attempt_id=None,
            apply_id=None, idempotency_key=None, commit_revision=None, active_revision=1, pending_revision=None,
            source=f"private-{index}", result="applied",
        ))
    path = "/api/gateway/config/events/stream" if gateway_owned else "/api/v1/config/events/stream"
    body = await _consume_sse_with_revocation(case, path)
    assert body.count(b"event: config\n") == 1 and b"private-1" in body and b"private-2" not in body
    error_frame = body.split(b"event: error\ndata: ")[1].split(b"\n\n")[0]
    assert "configuration_read" in json.loads(error_frame)["message"]
    if not gateway_owned:
        assert stream.closed and case.registry.route_reference_counts(WORKSPACE_ID) == (0, 0)
    denied = await case.client.get(path)
    assert denied.status_code == 403
