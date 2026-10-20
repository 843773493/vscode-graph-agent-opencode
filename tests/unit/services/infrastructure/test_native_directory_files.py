"""真实控制面绑定、文件 HTTP 入口与 URI 恢复合同。"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import workspace as workspace_api
from app.api.deps import get_workspace_service, verify_local_token
from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver
from app.core.resource_uri import parse_resource_uri
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes import native_directories
from app.gateway.routes._shared import get_registry
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceFileMutationIntentDTO,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.file_tree_settings_service import (
    FileTreeSettingsService,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.workspace_files.paths import (
    GatewayNativeDirectoryClient,
)
from app.services.infrastructure.workspace_service import WorkspaceService
from tests.harness.python.run_context import TestRunContext
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

GATEWAY_ID = "gateway_01900000000070008000000000000001"


@pytest.mark.parametrize("url", [
    "http://192.0.2.1:8014", "https://remote.example", "http://localhost.example",
    "http://127.0.0.1@remote.example", "http://user:secret@127.0.0.1:8014",
    "http://127.0.0.1:8014/path", "http://127.0.0.1:8014?query=1",
    "http://127.0.0.1:8014#fragment", "ftp://127.0.0.1:8014",
])
@pytest.mark.parametrize("operation", ["bind", "resolve"])
def test_native_client_rejects_nonlocal_url_before_credentials_or_transport(url, operation, monkeypatch):
    token = Mock(return_value="must-not-send")
    transport = Mock()
    monkeypatch.setattr(httpx, "Client", transport)
    client = GatewayNativeDirectoryClient(workspace_id="workspace",
        connection=GatewayTransportConnection(gateway_url=url), local_token_supplier=token)
    with pytest.raises(ValueError, match="同机 loopback"):
        if operation == "bind":
            client.bind(Path("/private/native-root"))
        else:
            client.resolve(parse_resource_uri(f"boxteam://gateway/{GATEWAY_ID}/resources/files/01900000000070008000000000000001"))
    token.assert_not_called()
    transport.assert_not_called()


@pytest.mark.parametrize("url", ["http://127.0.0.1:45123", "http://localhost:45123", "https://[::1]:45123"])
def test_native_client_rechecks_reloaded_gateway_before_sending(url, monkeypatch):
    requests = []
    uri = f"boxteam://gateway/{GATEWAY_ID}/resources/files/01900000000070008000000000000001"
    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"X-Request-ID": "req_native_local"},
            json={"data": {"uri": uri}, "request_id": "req_native_local"})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(respond)))
    config = Mock(spec=ConfigService)
    config.get_gateway_connection_url.return_value = url
    config.get_gateway_connection_timeout_seconds.return_value = 5
    token = Mock(return_value="local-token")
    client = GatewayNativeDirectoryClient(workspace_id="workspace",
        connection=GatewayTransportConnection(config_service=config), local_token_supplier=token)
    assert client.bind(Path("/private/native-root")) == uri
    assert len(requests) == 1 and requests[0].url.port == 45123
    assert requests[0].headers["X-Local-Token"] == "local-token"
    config.get_gateway_connection_url.return_value = "http://remote.example:45123"
    with pytest.raises(ValueError, match="同机 loopback"):
        client.bind(Path("/private/native-root"))
    token.assert_called_once()
    assert len(requests) == 1


def test_native_client_does_not_follow_remote_redirect(monkeypatch):
    requests = []
    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "http://remote.example/native"})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(redirect)))
    client = GatewayNativeDirectoryClient(workspace_id="workspace",
        connection=GatewayTransportConnection(gateway_url="http://127.0.0.1:45123"),
        local_token_supplier=lambda: "local-token")
    with pytest.raises(httpx.HTTPStatusError):
        client.bind(Path("/private/native-root"))
    assert len(requests) == 1 and requests[0].url.host == "127.0.0.1"


@pytest.fixture
async def native_case(request, monkeypatch):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = context.workspace_root
    workspace.mkdir(parents=True, exist_ok=True)
    external = workspace.parent / "未注册 目录"
    shutil.rmtree(external, ignore_errors=True)
    external.mkdir()
    (external / "文件 %.txt").write_text("external", encoding="utf-8")
    (workspace / "same.txt").write_text("workspace", encoding="utf-8")
    config = Mock(spec=ConfigService)
    config.get_workspace_file_default_limit.return_value = 500
    config.get_workspace_preview_max_bytes.return_value = 1024 * 1024
    config.get_workspace_preview_binary_sample_bytes.return_value = 8192
    identity = WorkspaceService(config_service=config, workspace_root=workspace).workspace_id
    state = GatewayStateStore(path=workspace.parent / "gateway/gateway.sqlite")
    registry_path = workspace.parent / "gateway/registry.json"
    registry = GatewayWorkspaceRegistry(storage_path=registry_path, state_store=state)
    registry.upsert(WorkspaceTarget(workspace_id="local-files", name="测试文件 owner",
        root_path=str(workspace), backend_url="http://127.0.0.1:45124", connection_kind="local",
        backend_workspace_id=identity))
    gateway = FastAPI()
    gateway.add_middleware(TraceMiddleware)
    gateway.include_router(native_directories.router)
    gateway.state.gateway_id = GATEWAY_ID
    gateway.state.gateway_state = state
    gateway.dependency_overrides[get_registry] = lambda: case.registry
    gateway_client = TestClient(gateway, client=("127.0.0.1", 45123))
    original_client = httpx.Client
    def control_transport(request):
        return gateway_client.request(request.method, str(request.url), content=request.content, headers=request.headers)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(control_transport)))
    def make_service():
        bindings = GatewayNativeDirectoryClient(workspace_id=identity,
            connection=GatewayTransportConnection(gateway_url="http://127.0.0.1:45123"),
            local_token_supplier=get_gateway_local_token)
        return WorkspaceService(config_service=config, workspace_root=workspace, native_directories=bindings)
    service = make_service()
    case = SimpleNamespace(context=context, workspace=workspace, external=external, state=state,
                           service=service, make_service=make_service, gateway_client=gateway_client,
                           gateway=gateway, registry=registry, registry_path=registry_path)
    await service.start()
    yield case
    await case.service.stop()
    case.service.close()
    gateway_client.close()
    case.registry.close()
    case.state.close()
    context.close()


async def test_native_binding_requires_token_loopback_and_local_workspace(native_case):
    case = native_case
    base = f"/api/gateway/native-directories/workspaces/{case.service.workspace_id}"
    body = {"native_root": str(case.external)}
    for token in ("", "wrong-token"):
        response = case.gateway_client.post(base, json=body, headers={"X-Local-Token": token})
        assert response.status_code == 401, response.text
    headers = {"X-Local-Token": get_gateway_local_token()}
    response = case.gateway_client.post(base, json=body, headers=headers)
    assert response.status_code == 200, response.text
    uri = response.json()["data"]["uri"]
    assert case.gateway_client.post(base, json=body, headers=headers).json()["data"]["uri"] == uri
    assert str(case.external) not in response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert case.gateway_client.post(base.replace(case.service.workspace_id, "unregistered"), json=body, headers=headers).status_code == 403
    with TestClient(case.gateway, client=("192.0.2.1", 45123)) as foreign_client:
        assert foreign_client.post(base, json=body, headers=headers).status_code == 403
    directory_id = parse_resource_uri(uri).tail_segments[0]
    assert case.gateway_client.get(f"{base}/gateway_other/{directory_id}", headers=headers).status_code == 409
    assert case.gateway_client.get(f"{base}/{GATEWAY_ID}/019000000000700080000000000000ff", headers=headers).status_code == 404


async def test_native_directory_http_edit_shortcut_and_operation_restart(native_case):
    case = native_case
    registered = case.registry.targets()
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(workspace_api.router, prefix="/api/v1")
    app.dependency_overrides[get_workspace_service] = lambda: case.service
    app.dependency_overrides[verify_local_token] = lambda: "authenticated"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend.test") as client:
        response = await client.get("/api/v1/workspace/files", params={"path": str(case.external)})
        assert response.status_code == 200, response.text
        listing = response.json()["data"]
        uri = listing["path"]
        parsed = parse_resource_uri(uri)
        assert parsed.scope == "gateway" and parsed.scope_id == GATEWAY_ID
        assert parsed.tail_segments[0][12] == "7"
        assert listing["directory_key"]["root"] == uri
        file_uri = listing["items"][0]["path"]
        opened = (await client.get("/api/v1/workspace/files/content", params={"path": file_uri})).json()["data"]
        saved = await client.put("/api/v1/workspace/files/content", params={"path": file_uri},
                                 json={"content": "edited", "expected_revision": opened["revision"]})
        assert saved.status_code == 200, saved.text
        assert (case.external / "文件 %.txt").read_text(encoding="utf-8") == "edited"
        assert saved.json()["data"]["path"] == file_uri
        assert str(case.external) not in json.dumps(listing, ensure_ascii=False)
        resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
        resolver.initialize()
        session_id = "ses_01900000000070008000000000000002"
        seed_catalog_session_bundle(case.workspace / ".boxteam/sessions", session_id, title="目录重开")
        settings = FileTreeSettingsService(workspace_root=case.workspace, path_resolver=resolver,
                                           file_paths=case.service.file_paths)
        shortcut = await asyncio.to_thread(settings.add_session_shortcut, session_id,
                                           path=str(case.external), label="未注册")
        assert shortcut.effective_shortcuts[0].path == uri
        listing = (await client.get("/api/v1/workspace/files", params={"path": uri})).json()["data"]
        intent = WorkspaceFileMutationIntentDTO(client_operation_id=create_prefixed_id("op"),
            client_sequence=1, directory_key=listing["directory_key"],
            expected_directory_revision=listing["directory_revision"])
        accepted = await client.post("/api/v1/workspace/files/entries", params={"path": uri},
            json={"name": "created.txt", "kind": "file", "intent": intent.model_dump()})
        assert accepted.status_code == 202, accepted.text
        operation_id = accepted.json()["data"]["operation_id"]
        async with asyncio.timeout(5):
            while case.service.file_operations.status([operation_id], actor="local").operations[0].state in {"queued", "running"}:
                await asyncio.sleep(0.01)
        receipt = case.service.file_operations.status([operation_id], actor="local").operations[0]
        assert receipt.state == "committed" and (case.external / "created.txt").is_file()
        await case.service.stop()
        case.service.close()
        case.registry.close()
        case.state.close()
        case.state = GatewayStateStore(path=case.workspace.parent / "gateway/gateway.sqlite")
        case.registry = GatewayWorkspaceRegistry(storage_path=case.registry_path, state_store=case.state)
        case.gateway.state.gateway_state = case.state
        assert case.registry.targets() == registered
        case.service = case.make_service()
        await case.service.start()
        reopened = await client.get("/api/v1/workspace/files/content", params={"path": settings.get(session_id).effective_shortcuts[0].path + "/created.txt"})
        assert reopened.status_code == 200, reopened.text
        assert case.service.file_operations.status([operation_id], actor="local").operations[0] == receipt
        assert not (case.external / ".boxteam").exists()
        with case.state.connection() as connection:
            roots = connection.execute("SELECT gateway_id,directory_id,native_root FROM native_directory_roots").fetchall()
            assert [(row[0], row[2]) for row in roots] == [(GATEWAY_ID, str(case.external))]
        with case.service.file_operations.store._lock:
            dump = "\n".join(case.service.file_operations.store._db.iterdump())
        assert str(case.external) not in dump and str(case.workspace) not in dump
        assert "real_path" not in dump
        assert case.registry.targets() == registered


async def test_three_inputs_same_file_and_external_absolute_never_redirected(native_case):
    case = native_case
    relative = await case.service.get_file_content(path="same.txt")
    absolute = await case.service.get_file_content(path=str(case.workspace / "same.txt"))
    addressed = await case.service.get_file_content(path=relative.path)
    assert relative == absolute == addressed
    # 工作区内部构造同名绝对路径片段，防止旧 lstrip("/") 回退看似读成功。
    external_file = case.external / "文件 %.txt"
    decoy = case.workspace / str(external_file).lstrip("/")
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text("wrong target", encoding="utf-8")
    external = await case.service.get_file_content(path=str(external_file))
    assert external.content == "external"
    await case.service.update_file_content(path=str(external_file), content="right target", expected_revision=external.revision)
    assert external_file.read_text(encoding="utf-8") == "right target"
    assert decoy.read_text(encoding="utf-8") == "wrong target"
    missing = external.path.replace(parse_resource_uri(external.path).tail_segments[0], "019000000000700080000000000000ff")
    with pytest.raises(httpx.HTTPStatusError):
        await case.service.get_file_content(path=missing)


async def test_reference_http_freezes_native_sources_as_uri(native_case):
    case = native_case
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(workspace_api.router, prefix="/api/v1")
    app.dependency_overrides[get_workspace_service] = lambda: case.service
    app.dependency_overrides[verify_local_token] = lambda: "authenticated"
    addressed = await case.service.get_file_content(path="same.txt")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend.test") as client:
        response = await client.post("/api/v1/workspace/files/references", json={"paths": [
            "same.txt", str(case.workspace / "same.txt"), addressed.path, str(case.external / "文件 %.txt")]} )
    assert response.status_code == 200, response.text
    uris = response.json()["data"]["uris"]
    assert uris[:3] == [addressed.path] * 3
    assert (await case.service.get_file_content(path=uris[3])).content == "external"
    assert str(case.workspace) not in response.text and str(case.external) not in response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]


async def test_native_directory_public_navigation_reuses_bound_file_paths(native_case):
    from app.api import resources as resource_api
    from app.api.deps import get_resource_navigation
    from app.services.infrastructure.resource_platform.sources.resource_navigation import (
        ResourceNavigation,
    )

    case = native_case
    registered = case.registry.targets()
    navigation = ResourceNavigation(workspace_root=case.workspace, workspace_id=case.service.workspace_id,
                                    file_paths=case.service.file_paths)
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(resource_api.router, prefix="/api/v1")
    app.dependency_overrides[get_resource_navigation] = lambda: navigation
    app.dependency_overrides[verify_local_token] = lambda: "authenticated"
    uri = (await case.service.file_references([str(case.external)])).uris[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend.test",
                                headers={"X-BoxTeam-Gateway-Id": GATEWAY_ID}) as client:
        response = await client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "list"})
        assert response.status_code == 200, response.text
        listed = response.json()["data"]
        assert listed["node"]["uri"] == uri
        child = listed["entries"][0]["uri"]
        response = await client.post("/api/v1/resources/navigation", json={"uri": child, "operation": "read"})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["content"] == "external"
        assert str(case.external) not in response.text
    assert case.registry.targets() == registered
    assert not (case.external / ".boxteam").exists()
