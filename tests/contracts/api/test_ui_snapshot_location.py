"""快照公开引用经过真实 HTTP 保存和读取，并绑定明确的工作区与会话。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.runtime import router
from app.core.path_utils import get_gateway_root, get_session_path_resolver
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.credentials import FederationCredentialStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.server.workspace_proxy import router as proxy_router
from app.gateway.server.workspace_proxy import verify_user_access_for_proxy
from app.services.infrastructure.log_service import LogService

SESSION_ID = "ses_019c257bff2c7c6989d1c38a6ae7fbf2"


@pytest.fixture
def snapshot_endpoint(tmp_path: Path, session_bundle_factory) -> Iterator[tuple[TestClient, Path, Path, str]]:
    sessions_root = tmp_path / ".boxteam" / "sessions"
    session_root = session_bundle_factory(sessions_root, SESSION_ID)
    resolver = get_session_path_resolver(sessions_root)
    service = LogService(workspace_root=tmp_path, path_resolver=resolver)
    container = SimpleNamespace(log_service=service)

    class Registry:
        def get(self, workspace_id: str):
            if workspace_id != resolver.workspace_id:
                raise KeyError(workspace_id)
            return container

    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.state.workspace_container_registry = Registry()
    application.include_router(router, prefix="/api/v1/workspaces/{workspace_id}")
    with TestClient(application, headers={"x-local-token": "local-dev-token"}) as client:
        yield client, tmp_path, session_root, resolver.workspace_id


@pytest.mark.parametrize("session_id", [None, SESSION_ID])
def test_snapshot_http_roundtrip_has_no_storage_location(snapshot_endpoint, session_id: str | None) -> None:
    client, workspace_root, session_root, workspace_id = snapshot_endpoint
    response = client.post(f"/api/v1/workspaces/{workspace_id}/runtime/log-snapshot", json={
        "html": "<p>快照内容</p>", "session_id": session_id, "page_title": "会话快照",
    }, headers={"X-Request-ID": "snapshot-contract"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["request_id"] == response.headers["X-Request-ID"] == "snapshot-contract"
    result = payload["data"]
    assert set(result) == {"snapshot_id", "html_url", "metadata_url", "status"}
    assert result["status"] == "saved"
    assert str(workspace_root) not in response.text
    assert result["html_url"].startswith(f"/api/v1/workspaces/{workspace_id}/")
    html = client.get(result["html_url"])
    assert html.status_code == 200 and html.text == "<p>快照内容</p>"
    assert "sandbox" in html.headers["Content-Security-Policy"]
    metadata_response = client.get(result["metadata_url"])
    assert metadata_response.status_code == 200
    metadata = metadata_response.json()
    assert set(metadata) == {"snapshot_id", "workspace_id", "session_id", "page_title", "status", "source", "created_at"}
    assert metadata["snapshot_id"] == result["snapshot_id"]
    assert metadata["workspace_id"] == workspace_id and metadata["session_id"] == session_id
    assert str(workspace_root) not in metadata_response.text
    directory = (session_root if session_id else workspace_root / ".boxteam") / "logs" / "webview"
    assert (directory / f'{result["snapshot_id"]}.html').read_text() == html.text
    assert json.loads((directory / f'{result["snapshot_id"]}.json').read_text()) == metadata
    if session_id:
        assert client.get(result["html_url"].split("?")[0]).status_code == 404


def test_snapshot_rejects_root_override_and_unknown_identity(snapshot_endpoint) -> None:
    client, workspace_root, _, workspace_id = snapshot_endpoint
    base = f"/api/v1/workspaces/{workspace_id}/runtime/log-snapshot"
    assert client.post(base, json={"html": "<p>x</p>", "workspace_root": str(workspace_root / "escape")}).status_code == 422
    assert not (workspace_root / "escape").exists()
    missing = client.post(base, json={"html": "<p>x</p>", "session_id": "ses_019c257bff2c7c6989d1c38a6ae7fbf3"})
    assert missing.status_code == 404
    assert client.get(f"{base}/bad-id/html").status_code == 400
    assert client.get(f"{base}/snapshot_019c257bff2c7c6989d1c38a6ae7fbf3/html").status_code == 404
    assert client.get(base.replace(workspace_id, "00000000-0000-4000-8000-000000000099") + "/bad-id/html").status_code == 404


def test_snapshot_corrupt_metadata_is_explicit_server_failure(snapshot_endpoint) -> None:
    client, workspace_root, _, workspace_id = snapshot_endpoint
    response = client.post(f"/api/v1/workspaces/{workspace_id}/runtime/log-snapshot", json={"html": "<p>x</p>"})
    result = response.json()["data"]
    path = workspace_root / ".boxteam" / "logs" / "webview" / f'{result["snapshot_id"]}.json'
    path.write_text("{")
    failure = client.get(result["metadata_url"])
    assert failure.status_code == 500
    assert "metadata 损坏" in failure.text


def test_snapshot_wire_roundtrip_and_http_contract_reserve_removed_location() -> None:
    from google.protobuf import descriptor_pb2

    from app.main import app
    from app.protocol.generated.boxteam.workspace.v2 import public_pb2

    value = public_pb2.UiSnapshotResultDTO(
        snapshot_id="snapshot_019c257bff2c7c6989d1c38a6ae7fbf2",
        html_url="/api/v1/workspaces/explicit/runtime/log-snapshot/snapshot_id/html",
        metadata_url="/api/v1/workspaces/explicit/runtime/log-snapshot/snapshot_id/metadata",
        status="saved",
    )
    assert value.FromString(value.SerializeToString()) == value
    assert not hasattr(public_pb2, "LogSnapshotResultDTO")
    shape = descriptor_pb2.DescriptorProto()
    value.DESCRIPTOR.CopyToProto(shape)
    assert "html_path" in shape.reserved_name
    assert any(span.start <= 1 < span.end for span in shape.reserved_range)
    schema = app.openapi()
    request = schema["components"]["schemas"]["UiSnapshotRequest"]
    assert "workspace_root" not in request["properties"] and request["additionalProperties"] is False
    result = schema["components"]["schemas"]["UiSnapshotResultDTO"]
    assert set(result["properties"]) == {"snapshot_id", "html_url", "metadata_url", "status"}
    html = schema["paths"]["/api/v1/workspaces/{workspace_id}/runtime/log-snapshot/{snapshot_id}/html"]["get"]
    assert "text/html" in html["responses"]["200"]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_gateway", [False, True])
async def test_snapshot_gateway_roundtrip_stays_on_explicit_workspace(
    snapshot_endpoint, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_gateway: bool,
) -> None:
    backend_client, workspace_root, _, workspace_id = snapshot_endpoint
    registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "gateway" / "targets.json")
    for target_id, backend_id in (("snapshot-target", workspace_id), ("active-target", "00000000-0000-4000-8000-000000000099")):
        registry.upsert(WorkspaceTarget(
            workspace_id=target_id, backend_workspace_id=backend_id, name=target_id,
            root_path=str(workspace_root), backend_url="http://backend.test", connection_kind="local",
        ), activate=True)
    gateway = FastAPI()
    gateway.add_middleware(TraceMiddleware)
    gateway.include_router(proxy_router)
    gateway.state.registry = registry
    gateway.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
    gateway.dependency_overrides[verify_user_access_for_proxy] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend_client.app), trust_env=False) as upstream:
        gateway.state.http_client = gateway.state.streaming_http_client = upstream
        if remote_gateway:
            monkeypatch.setenv("BOXTEAM_GATEWAY_ROOT", str(tmp_path / "gateway-root"))
            FederationCredentialStore(storage_path=get_gateway_root() / "credentials" / "federation.json").issue(
                connection_id="remote-connection", peer_gateway_id="gateway_019c257bff2c7c6989d1c38a6ae7fbf2",
            )
            gateway.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(
                kind="federation", peer_gateway_id="gateway_019c257bff2c7c6989d1c38a6ae7fbf3",
            )
            projected_registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "projection" / "targets.json")
            projected_registry.upsert(WorkspaceTarget(
                workspace_id="remote-projection", backend_workspace_id=workspace_id, name="remote",
                root_path=None, backend_url="http://remote-gateway.test", connection_kind="remote_gateway",
                remote_gateway_connection_id="remote-connection", remote_workspace_id="snapshot-target",
            ), activate=False)
            projected_registry.upsert(WorkspaceTarget(
                workspace_id="active-target", backend_workspace_id="00000000-0000-4000-8000-000000000099",
                name="active", root_path=str(workspace_root), backend_url="http://unused.test", connection_kind="local",
            ), activate=True)
            projected_registry._remote_gateway_runtimes["remote-connection"] = SimpleNamespace(
                service_urls={"workspace_api": "http://remote-gateway.test"},
            )
            entry = FastAPI()
            entry.add_middleware(TraceMiddleware)
            entry.include_router(proxy_router)
            entry.state.registry = projected_registry
            entry.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
            entry.dependency_overrides[verify_user_access_for_proxy] = lambda: None
        else:
            entry = gateway
        # 两层都走真实代理与后端路由；transport 仅替换网络连接。
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), trust_env=False) as remote_upstream:
            if remote_gateway:
                entry.state.http_client = entry.state.streaming_http_client = remote_upstream
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=entry), base_url="http://gateway.test") as client:
                response = await client.post(f"/api/v1/workspaces/{workspace_id}/runtime/log-snapshot", json={
                    "html": "<p>经过 Gateway 的快照</p>", "session_id": SESSION_ID,
                }, headers={"X-Request-ID": "snapshot-through-gateway"})
                assert response.status_code == 200, response.text
                assert response.json()["request_id"] == response.headers["X-Request-ID"] == "snapshot-through-gateway"
                result = response.json()["data"]
                html = await client.get(result["html_url"])
                assert html.status_code == 200 and html.text == "<p>经过 Gateway 的快照</p>"
                metadata = await client.get(result["metadata_url"])
                assert metadata.status_code == 200 and metadata.json()["workspace_id"] == workspace_id
                assert str(workspace_root) not in metadata.text + response.text
                assert registry.active_workspace_id == "active-target"
