"""实际 Gateway→Backend ASGI 候选链；受控传输，不启动进程或下载包。"""

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
from app.gateway.federation.policy import Capability, FederationPolicyStore
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


@pytest.mark.parametrize("mount", ["workspace", "active"])
@pytest.mark.parametrize("method,suffix", [("GET", "installation-capabilities"), ("GET", "installations"),
    ("GET", "installation-operations/original"), ("POST", "installation-operations/original/cancel"), ("POST", "installations"),
    ("GET", "installations/original/references"), ("DELETE", "installations/original")])
async def test_artifact_routes_require_installation_permission_before_backend(mcp_access: McpAccess, mount: str, method: str, suffix: str) -> None:
    response = await mcp_access.client.request(method, mcp_access.path(mount, suffix), json={})
    assert response.status_code == 403 and "mcp_installation_manage" in response.text
    assert mcp_access.calls == []


@dataclass
class McpAccess:
    client: httpx.AsyncClient
    registry: GatewayWorkspaceRegistry
    policy: FederationPolicyStore
    owner: McpCatalogOwner
    workspace_id: str
    target: dict[str, str]
    calls: list[tuple[str, str]] = field(default_factory=list)
    before_response: Callable[[], None] | None = None

    def allow(self, *capabilities: Capability) -> None:
        self.policy.publish({"rules": [{"capability": capability, "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}
                                       for capability in capabilities]})

    def request(self) -> dict[str, object]:
        return {"target": self.target, "source": {"kind": "manual", "name": "same", "version": "1",
                "definition": {"remotes": [{"type": "streamable-http", "url": "https://service.test/mcp"}]}}}

    def path(self, mount: str, suffix: str = "candidate-manifests") -> str:
        prefix = f"workspaces/{self.workspace_id}/" if mount == "workspace" else ""
        return f"/api/v1/{prefix}mcp/{suffix}"


@pytest.fixture
async def mcp_access(
    tmp_path: Path, empty_workspace_scope: tuple[str, Path], monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[McpAccess]:
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
    registry.upsert(WorkspaceTarget(gateway_id=gateway_id, workspace_id=workspace_id, name="原生工作区", root_path=str(root),
                                   backend_url="http://backend.test", connection_kind="local"),
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
                headers={"X-BoxTeam-Federation-Token": token, "X-Request-ID": "mcp-installation-access"}) as client:
                case = McpAccess(client, registry, policy, owner, workspace_id,
                                 {"kind": "backend", "gateway_id": gateway_id, "backend_id": backend_id})

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


@pytest.mark.parametrize("mount", ["direct", "workspace"])
@pytest.mark.parametrize("capability", ["discovery", "read", "send", "managed_workspace_admin", "configuration_read", "configuration_write"])
@pytest.mark.parametrize("suffix", ["candidate-manifests", "candidate-plans", "candidate-plans/validate"])
async def test_other_capabilities_never_forward_installation_management(
    mcp_access: McpAccess, mount: str, capability: Capability, suffix: str,
) -> None:
    case = mcp_access
    case.allow(capability)
    response = await case.client.post(case.path(mount, suffix), json=case.request(), headers={"X-BoxTeam-Gateway-Id": case.target["gateway_id"]})
    assert response.status_code == 403 and "mcp_installation_manage" in response.text
    assert response.json()["request_id"] == response.headers["X-Request-ID"] == "mcp-installation-access"
    assert not case.calls and case.registry.route_reference_counts(case.workspace_id) == (0, 0)


@pytest.mark.parametrize("binding", [{}, {"origin_gateway_id": PEER}, {"principal_ref": principal_ref_for_gateway("other")}])
async def test_wildcard_or_other_principal_does_not_grant_installation(mcp_access: McpAccess, binding: dict[str, str]) -> None:
    case = mcp_access
    case.policy.publish({"rules": [{"capability": "mcp_installation_manage", "effect": "allow", **binding}]})
    response = await case.client.post(case.path("workspace"), json=case.request())
    assert response.status_code == 403 and not case.calls


@pytest.mark.parametrize("mount", ["direct", "workspace"])
async def test_exact_native_authorization_reaches_same_owner_and_full_plan(mcp_access: McpAccess, mount: str) -> None:
    case = mcp_access
    case.allow("mcp_installation_manage")
    inspect = await case.client.post(case.path(mount), json=case.request())
    assert inspect.status_code == 200, inspect.text
    doc = inspect.json()["data"]
    request = {**case.request(), "manifest_digest": doc["manifest_digest"], "variant": doc["variants"][0]["ref"]}
    response = await case.client.post(case.path(mount, "candidate-plans"), json=request)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target"] == case.target and body["data"]["disposition"] == "configuration_only"
    assert body["data"]["download_required"] is False
    assert body["request_id"] == response.headers["X-Request-ID"] == "mcp-installation-access"
    verified = await case.client.post(case.path(mount, "candidate-plans/validate"),
                                     json={"candidate": request, "plan_digest": body["data"]["plan_digest"]})
    assert verified.status_code == 200 and verified.json()["data"] == body["data"]
    assert case.calls == [("POST", case.path(mount)), ("POST", case.path(mount, "candidate-plans")),
                          ("POST", case.path(mount, "candidate-plans/validate"))]
    case.policy.publish({"rules": []})
    denied = await case.client.post(case.path(mount, "candidate-plans"), json=request)
    assert denied.status_code == 403 and len(case.calls) == 3
    assert case.registry.route_reference_counts(case.workspace_id) == (0, 0)


async def test_permission_revoked_while_waiting_prevents_first_backend_request(mcp_access: McpAccess, monkeypatch: pytest.MonkeyPatch) -> None:
    case = mcp_access
    case.allow("mcp_installation_manage")

    async def wait(request: Request, target: WorkspaceTarget) -> None:
        case.policy.publish({"rules": []})

    monkeypatch.setattr(workspace_proxy, "_wait_for_workspace_runtime", wait)
    response = await case.client.post(case.path("workspace"), json=case.request())
    assert response.status_code == 403 and not case.calls
    assert case.registry.route_reference_counts(case.workspace_id) == (0, 0)


async def test_native_revocation_before_disclosure_hides_candidate(mcp_access: McpAccess) -> None:
    case = mcp_access
    case.allow("mcp_installation_manage")

    def revoke() -> None:
        case.policy.publish({"rules": []})

    case.before_response = revoke
    response = await case.client.post(case.path("direct"), json=case.request())
    assert response.status_code == 403 and "manifest_digest" not in response.text
    assert len(case.calls) == 1 and case.registry.route_reference_counts(case.workspace_id) == (0, 0)


@pytest.mark.parametrize("path", ["mcp/%2563andidate-plans", "mcp/candidate-plans/validate",
                                "mcp/unused/%252e%252e/candidate-plans"])
async def test_encoded_management_paths_cannot_bypass_native_capability(mcp_access: McpAccess, path: str) -> None:
    case = mcp_access
    response = await case.client.post(f"/api/v1/workspaces/{case.workspace_id}/{path}", json=case.request())
    assert response.status_code in {400, 403} and not case.calls


def _private_request(case: McpAccess) -> dict[str, object]:
    return {"target": {"kind": "workspace", "gateway_id": case.target["gateway_id"], "workspace_id": case.workspace_id},
            "server_id": "secure", "header_name": "Authorization", "endpoint": "https://service.test/mcp",
            "value": "Bearer private-gateway-unit-only"}


async def test_manual_env_uses_configuration_permissions_without_installation_permission(mcp_access: McpAccess) -> None:
    case = mcp_access
    case.allow("configuration_read", "configuration_write")
    payload = {"target": _private_request(case)["target"], "server_id": "secure", "input_key": "API_KEY",
               "launch": {"command": "/tools/server", "args": []}, "value": "private-env-gateway-unit"}
    response = await case.client.post(case.path("workspace", "private-env"), json=payload)
    assert response.status_code == 200 and payload["value"] not in response.text
    assert response.json()["data"]["input_key"] == "API_KEY"
    case.before_response = lambda: case.allow("configuration_read")
    revoked = await case.client.post(case.path("workspace", "private-env"), json=payload)
    assert revoked.status_code == 403 and "definition_digest" not in revoked.text
    assert case.registry.route_reference_counts(case.workspace_id) == (0, 0)


@pytest.mark.parametrize("method,path", [("POST", "candidate-plans/private-env"), ("PUT", "candidate-configurations")])
async def test_catalog_secret_and_configuration_also_require_installation_before_backend(mcp_access: McpAccess, method: str, path: str) -> None:
    case = mcp_access
    case.allow("configuration_read", "configuration_write")
    response = await case.client.request(method, case.path("workspace", path), json={})
    assert response.status_code == 403 and "mcp_installation_manage" in response.text and not case.calls


@pytest.mark.parametrize("mount", ["direct", "workspace"])
@pytest.mark.parametrize("capability", ["read", "configuration_read", "configuration_write", "mcp_installation_manage"])
async def test_private_header_requires_configuration_read_and_write_before_backend(
    mcp_access: McpAccess, mount: str, capability: Capability,
) -> None:
    case = mcp_access
    case.allow(capability)
    response = await case.client.post(case.path(mount, "private-headers"), json=_private_request(case))
    assert response.status_code == 403 and "configuration_" in response.text
    assert not case.calls and case.registry.route_reference_counts(case.workspace_id) == (0, 0)


@pytest.mark.parametrize("mount", ["direct", "workspace"])
async def test_private_header_transparently_routes_to_exact_workspace_and_returns_no_value(mcp_access: McpAccess, mount: str) -> None:
    case = mcp_access
    case.allow("configuration_read", "configuration_write")
    payload = _private_request(case)
    response = await case.client.post(case.path(mount, "private-headers"), json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target"] == payload["target"] and body["data"]["workspace_id"] == case.workspace_id
    assert body["data"]["endpoint"] == payload["endpoint"] and body["data"]["header_name"] == payload["header_name"]
    assert response.headers["Cache-Control"] == "no-store"
    assert body["request_id"] == response.headers["X-Request-ID"] == "mcp-installation-access"
    assert payload["value"] not in response.text and "value" not in body["data"]
    assert case.calls == [("POST", case.path(mount, "private-headers"))]


@pytest.mark.parametrize("mount", ["direct", "workspace"])
async def test_private_header_rechecks_native_write_permission_after_runtime_wait(mcp_access: McpAccess, mount: str, monkeypatch) -> None:
    case = mcp_access
    case.allow("configuration_read", "configuration_write")

    async def wait(request: Request, target: WorkspaceTarget) -> None:
        case.allow("configuration_read")

    monkeypatch.setattr(workspace_proxy, "_wait_for_workspace_runtime", wait)
    response = await case.client.post(case.path(mount, "private-headers"), json=_private_request(case))
    assert response.status_code == 403 and "configuration_write" in response.text
    assert not case.calls and case.registry.route_reference_counts(case.workspace_id) == (0, 0)


@pytest.mark.parametrize("mount", ["direct", "workspace"])
async def test_private_header_rechecks_native_write_permission_before_disclosing_reference(mcp_access: McpAccess, mount: str) -> None:
    case = mcp_access
    case.allow("configuration_read", "configuration_write")
    case.before_response = lambda: case.allow("configuration_read")
    response = await case.client.post(case.path(mount, "private-headers"), json=_private_request(case))
    assert response.status_code == 403 and "configuration_write" in response.text and "version" not in response.text
    assert len(case.calls) == 1 and case.registry.route_reference_counts(case.workspace_id) == (0, 0)


@pytest.mark.parametrize("mount", ["direct", "workspace"])
@pytest.mark.parametrize("path", ["mcp/%2570rivate-headers", "mcp/unused/%252e%252e/private-headers"])
async def test_encoded_private_header_path_cannot_bypass_native_write_permission(mcp_access: McpAccess, mount: str, path: str) -> None:
    case = mcp_access
    case.allow("read")
    prefix = f"/api/v1/workspaces/{case.workspace_id}/" if mount == "workspace" else "/api/v1/"
    response = await case.client.post(prefix + path, json=_private_request(case))
    assert response.status_code in {400, 403} and not case.calls
