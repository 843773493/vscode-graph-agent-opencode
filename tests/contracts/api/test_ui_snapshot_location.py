"""快照公开引用经 ASGI 真实路由保存和读取，并绑定明确的工作区与会话。"""

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
from app.core.path_utils import get_session_path_resolver
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import bind_workspace_id, load_or_create_workspace_id
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.credentials import FederationCredentialStore, load_or_create_gateway_id
from app.gateway.federation import FEDERATION_PROTOCOL_VERSION, RemoteGatewayConnection
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.workspace_proxy import router as proxy_router
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


def test_snapshot_corrupt_metadata_is_explicit_server_failure(snapshot_endpoint, caplog: pytest.LogCaptureFixture) -> None:
    client, workspace_root, _, workspace_id = snapshot_endpoint
    response = client.post(f"/api/v1/workspaces/{workspace_id}/runtime/log-snapshot", json={"html": "<p>x</p>"})
    result = response.json()["data"]
    path = workspace_root / ".boxteam" / "logs" / "webview" / f'{result["snapshot_id"]}.json'
    path.write_text("{")
    failure = client.get(result["metadata_url"])
    assert failure.status_code == 500
    assert failure.json()["message"] == "请求处理失败: stage=http_request, error=RuntimeError"
    assert "metadata 损坏" in caplog.text


def test_snapshot_wire_roundtrip_and_http_contract_reserve_removed_location() -> None:
    from google.protobuf import descriptor_pb2

    from app.main import app
    from app.protocol.generated.boxteam.workspace.v2 import public_pb2

    workspace_id = "00000000-0000-4000-8000-000000000001"
    value = public_pb2.UiSnapshotResultDTO(
        snapshot_id="snapshot_019c257bff2c7c6989d1c38a6ae7fbf2",
        html_url=f"/api/v1/workspaces/{workspace_id}/runtime/log-snapshot/snapshot_id/html",
        metadata_url=f"/api/v1/workspaces/{workspace_id}/runtime/log-snapshot/snapshot_id/metadata",
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
    native_root = tmp_path / "gateway"
    native_gateway_id = load_or_create_gateway_id(native_root / "identity.json")
    active_root = tmp_path / "native-active-workspace"
    active_workspace_id = load_or_create_workspace_id(active_root)
    registry = GatewayWorkspaceRegistry(storage_path=native_root / "targets.json", gateway_id=native_gateway_id)
    for target_id, target_root in ((workspace_id, workspace_root), (active_workspace_id, active_root)):
        registry.upsert(WorkspaceTarget(
            gateway_id=native_gateway_id, workspace_id=target_id, name=target_id,
            root_path=str(target_root), backend_url="http://backend.test", connection_kind="local",
        ), activate=True)
    gateway = FastAPI()
    gateway.add_middleware(TraceMiddleware)
    gateway.include_router(proxy_router)
    gateway.state.registry = registry
    gateway.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")

    class GatewayTransport(httpx.ASGITransport):
        def __init__(self, application: FastAPI, gateway_root: Path) -> None:
            super().__init__(app=application)
            self.gateway_root = gateway_root
            self.requests: list[httpx.Request] = []

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            # 本测试串行请求，在 transport 边界模拟各 Gateway 独立进程的身份目录。
            with monkeypatch.context() as environment:
                environment.setenv("BOXTEAM_GATEWAY_ROOT", str(self.gateway_root))
                return await super().handle_async_request(request)

    backend_requests: list[httpx.Request] = []

    async def record_backend_request(request: httpx.Request) -> None:
        backend_requests.append(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend_client.app), trust_env=False,
        event_hooks={"request": [record_backend_request]},
    ) as upstream:
        gateway.state.http_client = gateway.state.streaming_http_client = upstream
        entry_registry = registry
        entry_gateway_id = native_gateway_id
        federation_token: str | None = None
        if remote_gateway:
            entry_root = tmp_path / "projection"
            entry_gateway_id = load_or_create_gateway_id(entry_root / "identity.json")
            federation_token = FederationCredentialStore(storage_path=entry_root / "credentials" / "federation.json").issue(
                connection_id="remote-connection", peer_gateway_id=entry_gateway_id,
            ).token
            gateway.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(
                kind="federation", peer_gateway_id=entry_gateway_id,
            )
            entry_registry = GatewayWorkspaceRegistry(storage_path=entry_root / "targets.json", gateway_id=entry_gateway_id)
            connection = RemoteGatewayConnection(
                connection_id="remote-connection", name="远端", host="remote.test", port=22, username="unit",
                private_key_path=None, ssh_config_host=None, remote_gateway_port=8014,
                remote_gateway_id=native_gateway_id, protocol_version=FEDERATION_PROTOCOL_VERSION,
                source_owner="manual", remote_config_event_cursor=1,
            )
            entry_registry.apply_remote_projection_snapshot(
                connection=connection,
                projections=(WorkspaceTarget(
                    gateway_id=native_gateway_id, workspace_id=workspace_id, name="remote", root_path=None,
                    backend_url="http://remote-gateway.test", connection_kind="remote_gateway", owner="remote_projection",
                    remote_gateway_connection_ids=(connection.connection_id,),
                ),),
                runtime=WorkspaceRuntime(service_urls={"workspace_api": "http://remote-gateway.test"}),
            )
            entry_active_root = tmp_path / "entry-active-workspace"
            bind_workspace_id(entry_active_root, workspace_id)
            entry_registry.upsert(WorkspaceTarget(
                gateway_id=entry_gateway_id, workspace_id=workspace_id, name="active",
                root_path=str(entry_active_root), backend_url="http://unused.test", connection_kind="local",
            ), activate=True)
            entry = FastAPI()
            entry.add_middleware(TraceMiddleware)
            entry.include_router(proxy_router)
            entry.state.registry = entry_registry
            entry.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
        else:
            entry = gateway
            entry_root = native_root
        # 两层都走真实代理与后端 ASGI 路由；transport 只替换网络和进程边界。
        remote_transport = GatewayTransport(gateway, native_root)
        async with httpx.AsyncClient(transport=remote_transport, trust_env=False) as remote_upstream:
            if remote_gateway:
                entry.state.http_client = entry.state.streaming_http_client = remote_upstream
            async with httpx.AsyncClient(
                transport=GatewayTransport(entry, entry_root), base_url="http://gateway.test", trust_env=False,
                headers={TARGET_GATEWAY_ID_HEADER: native_gateway_id},
            ) as client:
                snapshot_url = f"/api/v1/workspaces/{workspace_id}/runtime/log-snapshot"
                request_ids = ("snapshot-through-gateway", "snapshot-html-through-gateway", "snapshot-metadata-through-gateway")
                response = await client.post(snapshot_url, json={
                    "html": "<p>经过 Gateway 的快照</p>", "session_id": SESSION_ID,
                }, headers={"X-Request-ID": request_ids[0]})
                assert response.status_code == 200, response.text
                assert response.json()["request_id"] == response.headers["X-Request-ID"] == request_ids[0]
                result = response.json()["data"]
                assert all(result[key].startswith(f"/api/v1/workspaces/{workspace_id}/") for key in ("html_url", "metadata_url"))
                html = await client.get(result["html_url"], headers={"X-Request-ID": request_ids[1]})
                assert html.status_code == 200 and html.text == "<p>经过 Gateway 的快照</p>"
                assert html.headers["X-Request-ID"] == request_ids[1]
                metadata = await client.get(result["metadata_url"], headers={"X-Request-ID": request_ids[2]})
                assert metadata.status_code == 200 and metadata.json()["workspace_id"] == workspace_id
                assert metadata.headers["X-Request-ID"] == request_ids[2]
                assert str(workspace_root) not in metadata.text + response.text
                expected_routes = [
                    (method, httpx.URL(url).raw_path)
                    for method, url in (("POST", snapshot_url), ("GET", result["html_url"]), ("GET", result["metadata_url"]))
                ]
                assert [(request.method, request.url.raw_path) for request in backend_requests] == expected_routes
                assert all(request.url.host == "backend.test" for request in backend_requests)
                assert tuple(request.headers["X-Request-ID"] for request in backend_requests) == request_ids
                assert all(request.headers["X-BoxTeam-Workspace-Id"] == workspace_id for request in backend_requests)
                assert all(request.headers["X-BoxTeam-Gateway-Id"] == native_gateway_id for request in backend_requests)
                assert all(TARGET_GATEWAY_ID_HEADER not in request.headers for request in backend_requests)
                assert registry.active_workspace == (native_gateway_id, active_workspace_id)
                if remote_gateway:
                    assert [(request.method, request.url.raw_path) for request in remote_transport.requests] == expected_routes
                    assert all(request.url.host == "remote-gateway.test" for request in remote_transport.requests)
                    assert tuple(request.headers["X-Request-ID"] for request in remote_transport.requests) == request_ids
                    assert all(request.headers["X-BoxTeam-Workspace-Id"] == workspace_id for request in remote_transport.requests)
                    assert all(request.headers[TARGET_GATEWAY_ID_HEADER] == native_gateway_id for request in remote_transport.requests)
                    assert all(request.headers["X-BoxTeam-Gateway-Id"] == entry_gateway_id for request in remote_transport.requests)
                    assert all(request.headers["X-BoxTeam-Federation-Token"] == federation_token for request in remote_transport.requests)
                    assert entry_registry.active_workspace == (entry_gateway_id, workspace_id)
