"""显式联邦预检读取真实身份文件；普通目录发现不增加磁盘探测。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes._shared import get_registry
from app.gateway.routes.federation import router


@dataclass
class NativePreflight:
    client: httpx.AsyncClient
    root: Path
    identity: Path
    workspace_id: str
    registry: GatewayWorkspaceRegistry


@pytest.fixture
async def native_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[NativePreflight]:
    root = tmp_path / "gateway"
    monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_id = load_or_create_workspace_id(workspace)
    gateway_id = load_or_create_gateway_id(root / "identity.json")
    credential = FederationCredentialStore(storage_path=root / "credentials/federation.json").issue(
        connection_id="rgw_inbound", peer_gateway_id="gateway_" + "b" * 32,
    )
    with GatewayStateStore(path=root / "gateway.sqlite") as store:
        registry = GatewayWorkspaceRegistry(storage_path=root / "workspaces.json", state_store=store)
        registry.upsert(WorkspaceTarget(
            gateway_id=gateway_id, workspace_id=workspace_id, name="保留名称",
            root_path=str(workspace), connection_kind="local", backend_url="http://127.0.0.1:49999",
        ))
        app = FastAPI()
        app.add_middleware(TraceMiddleware)
        app.include_router(router)
        app.dependency_overrides[get_registry] = lambda: registry
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://gateway.test", headers={"X-BoxTeam-Federation-Token": credential.token}) as client:
            try:
                yield NativePreflight(client, root, workspace / ".boxteam/workspace-identity.json", workspace_id, registry)
            finally:
                registry.close()


@pytest.mark.asyncio
async def test_explicit_inventory_proof_uses_real_identity_and_preserves_request_id(native_preflight: NativePreflight) -> None:
    state = native_preflight
    before = state.identity.read_bytes()
    revision = state.registry.registry_revision
    response = await state.client.get("/api/gateway/federation/workspaces?verify_identity=true",
                                      headers={"X-Request-ID": "req_native_identity"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["identities_verified"] is True
    assert body["data"]["items"][0]["workspace_id"] == state.workspace_id
    assert body["data"]["gateway_id"] == state.registry.gateway_id
    assert body["request_id"] == response.headers["X-Request-ID"] == "req_native_identity"
    assert state.identity.read_bytes() == before
    assert state.registry.registry_revision == revision


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_changed_or_missing_native_identity_rejects_proof_without_initializing_or_rebinding(
    native_preflight: NativePreflight, missing: bool,
) -> None:
    state = native_preflight
    if missing:
        state.identity.unlink()
    else:
        payload = json.loads(state.identity.read_bytes())
        payload["workspace_id"] = "00000000-0000-4000-8000-000000000003"
        state.identity.write_text(json.dumps(payload))
    before = None if missing else state.identity.read_bytes()
    revision = state.registry.registry_revision
    ordinary = await state.client.get("/api/gateway/federation/workspaces")
    assert ordinary.status_code == 200, ordinary.text
    assert ordinary.json()["data"]["identities_verified"] is False
    response = await state.client.get("/api/gateway/federation/workspaces?verify_identity=true")
    assert response.status_code == 500, response.text
    assert (None if not state.identity.exists() else state.identity.read_bytes()) == before
    assert state.registry.registry_revision == revision
    assert state.registry.resolve(state.workspace_id).workspace_id == state.workspace_id


@pytest.mark.asyncio
async def test_registry_change_during_probe_cannot_label_new_inventory_verified(
    native_preflight: NativePreflight, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = native_preflight
    validate = state.registry.validate_local_root_bindings

    def changing(project_root: Path) -> None:
        validate(project_root)
        state.registry.rename(state.workspace_id, "核验期间修改")

    monkeypatch.setattr(state.registry, "validate_local_root_bindings", changing)
    response = await state.client.get("/api/gateway/federation/workspaces?verify_identity=true")
    assert response.status_code == 409, response.text
    assert "登记在身份核验期间变化" in response.json()["detail"]


@pytest.mark.asyncio
async def test_explicit_identity_probe_keeps_federation_authentication_boundary(native_preflight: NativePreflight) -> None:
    state = native_preflight
    before = state.identity.read_bytes()
    response = await state.client.get("/api/gateway/federation/workspaces?verify_identity=true",
                                      headers={"X-BoxTeam-Federation-Token": "invalid"})
    assert response.status_code == 401, response.text
    assert state.identity.read_bytes() == before
