"""MCP 私有 header 的 Gateway 路由与配置权限边界。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request
from starlette.responses import Response

from app.api import mcp
from app.api.deps import (
    get_mcp_catalog_owner,
    get_mcp_connection_service,
    verify_local_token,
)
from app.core.backend_identity import load_or_create_backend_id
from app.core.trace_middleware import TraceMiddleware
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.main import gateway_http_exception_handler
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server import workspace_proxy
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner
from app.services.infrastructure.mcp.connection_service import McpConnectionService
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

PEER = "gateway_" + "a" * 32


@dataclass
class PrivateHeaderAccess:
    client: httpx.AsyncClient
    registry: GatewayWorkspaceRegistry
    policy: FederationPolicyStore
    owner: McpCatalogOwner
    workspace_id: str
    gateway_id: str
    backend_id: str
    calls: list[tuple[str, str]] = field(default_factory=list)
    before_response: Callable[[], None] | None = None

    def allow(self, *capabilities: str) -> None:
        self.policy.publish({"rules": [{"capability": capability, "effect": "allow",
            "principal_ref": principal_ref_for_gateway(PEER)} for capability in capabilities]})

    @property
    def path(self) -> str:
        return f"/api/v1/workspaces/{self.workspace_id}/mcp/private-headers"

    def request(self) -> dict[str, object]:
        return {"target": {"kind": "workspace", "gateway_id": self.gateway_id, "workspace_id": self.workspace_id},
            "server_id": "secure", "header_name": "Authorization", "endpoint": "https://service.test/mcp",
            "value": "Bearer private-gateway-unit-only"}


@pytest.fixture
async def private_header_access(
    tmp_path: Path, empty_workspace_scope: tuple[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[PrivateHeaderAccess]:
    from app.main import app as workspace_app

    home = tmp_path / "home"
    gateway_root = home / "gateway"
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(gateway_root))
    gateway_id = load_or_create_gateway_id(gateway_root / "identity.json")
    backend_id = load_or_create_backend_id(home)
    workspace_id, root = empty_workspace_scope
    owner = McpCatalogOwner(raw_config={"servers": {}}, workspace_root=root,
        event_service=EventChannelService(), leases=ExternalResourceLeaseLedger())
    backend = FastAPI()
    backend.add_middleware(TraceMiddleware)
    backend.include_router(mcp.router, prefix="/api/v1")
    backend.include_router(mcp.router, prefix="/api/v1/workspaces/{workspace_id}")
    backend.state.backend_id = backend_id
    backend.dependency_overrides[get_mcp_catalog_owner] = lambda: owner
    connection_store = WorkspaceStateStore(workspace_root=root)
    connection_service = McpConnectionService(owner=owner, store=connection_store, workspace_id=workspace_id)
    backend.dependency_overrides[get_mcp_connection_service] = lambda: connection_service
    backend.dependency_overrides[verify_local_token] = lambda: "local"
    for error_type in (HTTPException, RequestValidationError):
        backend.add_exception_handler(error_type, workspace_app.exception_handlers[error_type])
    registry = GatewayWorkspaceRegistry(gateway_id=gateway_id, storage_path=gateway_root / "registry.json")
    registry.upsert(WorkspaceTarget(gateway_id=gateway_id, workspace_id=workspace_id, backend_id=backend_id,
        name="原生工作区", root_path=str(root), backend_url="http://backend.test", connection_kind="local"),
        runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://backend.test"}))
    token = FederationCredentialStore(storage_path=gateway_root / "credentials/federation.json").issue(
        connection_id="mcp-peer", peer_gateway_id=PEER,
    ).token
    policy = FederationPolicyStore()
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(workspace_proxy.router)

    async def http_error(request: Request, error: Exception) -> Response:
        assert isinstance(error, HTTPException)
        return await gateway_http_exception_handler(request, error)

    app.add_exception_handler(HTTPException, http_error)
    app.state.registry = registry
    app.state.gateway_id = gateway_id
    app.state.federation_policy_store = policy
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend)) as upstream:
            app.state.http_client = app.state.streaming_http_client = upstream
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://native-gateway.test",
                headers={"X-BoxTeam-Federation-Token": token, "X-Request-ID": "mcp-private-header-access"}) as client:
                case = PrivateHeaderAccess(client, registry, policy, owner, workspace_id, gateway_id, backend_id)

                @backend.middleware("http")
                async def observe(request: Request, call_next: Callable) -> Response:
                    case.calls.append((request.method, request.url.path))
                    response = await call_next(request)
                    if case.before_response is not None:
                        case.before_response()
                    return response

                yield case
                assert not owner.started and owner._session_scope is None
    finally:
        connection_store.close()
        registry.close()


@pytest.mark.parametrize("capability", ["read", "configuration_read", "configuration_write"])
async def test_private_header_requires_both_configuration_permissions_before_backend(
    private_header_access: PrivateHeaderAccess, capability: str,
) -> None:
    case = private_header_access
    case.allow(capability)
    response = await case.client.post(case.path, json=case.request())
    assert response.status_code == 403 and "configuration_" in response.text
    assert not case.calls and case.registry.route_reference_counts(case.workspace_id) == (0, 0)


async def test_private_header_routes_to_exact_workspace_and_returns_no_value(private_header_access: PrivateHeaderAccess) -> None:
    case = private_header_access
    case.allow("configuration_read", "configuration_write")
    payload = case.request()
    response = await case.client.post(case.path, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target"] == payload["target"] and body["data"]["workspace_id"] == case.workspace_id
    assert body["data"]["endpoint"] == payload["endpoint"] and body["data"]["header_name"] == payload["header_name"]
    assert response.headers["Cache-Control"] == "no-store"
    assert body["request_id"] == response.headers["X-Request-ID"] == "mcp-private-header-access"
    secret = payload["value"]
    assert isinstance(secret, str) and secret not in response.text and "value" not in body["data"]
    assert case.calls == [("POST", case.path)]


async def test_private_header_rechecks_write_permission_after_runtime_wait(
    private_header_access: PrivateHeaderAccess, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = private_header_access
    case.allow("configuration_read", "configuration_write")

    async def wait(request: Request, target: WorkspaceTarget) -> None:
        case.allow("configuration_read")

    monkeypatch.setattr(workspace_proxy, "_wait_for_workspace_runtime", wait)
    response = await case.client.post(case.path, json=case.request())
    assert response.status_code == 403 and "configuration_write" in response.text
    assert not case.calls and case.registry.route_reference_counts(case.workspace_id) == (0, 0)


async def test_private_header_rechecks_write_permission_before_disclosing_reference(
    private_header_access: PrivateHeaderAccess,
) -> None:
    case = private_header_access
    case.allow("configuration_read", "configuration_write")
    case.before_response = lambda: case.allow("configuration_read")
    response = await case.client.post(case.path, json=case.request())
    assert response.status_code == 403 and "configuration_write" in response.text and "version" not in response.text
    assert len(case.calls) == 1 and case.registry.route_reference_counts(case.workspace_id) == (0, 0)


@pytest.mark.parametrize("path", [
    "mcp/%2570rivate-headers",
    "mcp/unused/%252e%252e/private-headers",
])
async def test_encoded_private_header_path_cannot_bypass_write_permission(
    private_header_access: PrivateHeaderAccess, path: str,
) -> None:
    case = private_header_access
    case.allow("read")
    response = await case.client.post(f"/api/v1/workspaces/{case.workspace_id}/{path}", json=case.request())
    assert response.status_code in {400, 403} and not case.calls
