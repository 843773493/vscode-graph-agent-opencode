"""真实控制面绑定、文件 HTTP 入口与 URI 恢复合同。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.services.infrastructure.workspace_service as workspace_service_module
from app.api import workspace as workspace_api
from app.api.deps import get_workspace_service, verify_local_token
from app.core.exceptions import ForbiddenError
from app.core.identifier import create_prefixed_id
from app.core.path_utils import get_session_path_resolver
from app.core.resource_uri import file_uri_child, parse_resource_uri
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
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

GATEWAY_ID = "gateway_01900000000070008000000000000001"


@pytest.fixture
def native_reply(monkeypatch):
    uri = f"boxteam://gateway/{GATEWAY_ID}/resources/files/01900000000070008000000000000001"
    reply = SimpleNamespace(uri=uri, content=b"", header="req-native-protocol", requests=[])

    def respond(request):
        reply.requests.append(request)
        return httpx.Response(200, headers={"X-Request-ID": reply.header}, content=reply.content)

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(respond)))
    reply.client = GatewayNativeDirectoryClient(workspace_id="workspace",
        connection=GatewayTransportConnection(gateway_url="http://127.0.0.1:45123"),
        local_token_supplier=lambda: "local-token")
    return reply


@pytest.mark.parametrize("operation", ["bind", "resolve"])
@pytest.mark.parametrize("invalid", ["code", "data", "request_id", "header", "owner", "native"])
def test_native_reply_preserves_envelope_owner_and_absolute_root_checks(native_reply, operation, invalid):
    reply = native_reply
    root = Path("/private-native-root")
    data = {"uri": reply.uri}
    if operation == "resolve":
        data["native_root"] = str(root)
    payload = {"data": dict(data), "request_id": "req-native-protocol", "message": str(root)}
    if invalid == "code":
        payload["code"] = 9
    elif invalid == "data":
        payload["data"] = None
    elif invalid == "request_id":
        payload["request_id"] = "req-other"
    elif invalid == "header":
        reply.header = ""
    elif invalid == "owner":
        payload["data"]["uri"] = reply.uri.replace("files/019", "files/018") + "/child"
    elif operation == "bind":
        payload["data"]["uri"] = "boxteam://workspace/workspace/resources/files/root"
    else:
        payload["data"]["native_root"] = "relative/private-native-root"
    reply.content = json.dumps(payload).encode()

    def invoke():
        return reply.client.bind(root) if operation == "bind" else reply.client.resolve(parse_resource_uri(reply.uri))

    with pytest.raises(ValueError) as failure:
        invoke()
    assert "private-native-root" not in str(failure.value)
    assert len(reply.requests) == 1
    reply.header = "req-native-protocol"
    reply.content = json.dumps({"data": data, "request_id": reply.header}).encode()
    assert invoke() == (reply.uri if operation == "bind" else root)
    assert len(reply.requests) == 2


@pytest.mark.parametrize("operation", ["bind", "resolve"])
@pytest.mark.parametrize("invalid", ["data", "json", "extra_key", "message"])
@pytest.mark.parametrize("private", [
    "/private-native-root/secret", r"C:\private-native-root\secret", r"\\host\private-native-root\secret",
])
def test_native_reply_validation_keeps_raw_input_in_diagnostics_and_allows_retry(
    native_reply, caplog, operation, invalid, private,
):
    reply = native_reply
    root = Path("/private-native-root")
    data = {"uri": reply.uri}
    if operation == "resolve":
        data["native_root"] = str(root)
    payload = {"data": dict(data), "request_id": "req-native-protocol"}
    if invalid == "data":
        payload["data"]["uri" if operation == "bind" else "native_root"] = [private]
        rule = "string_type"
    elif invalid == "extra_key":
        payload["data"][private] = private
        rule = "extra_forbidden"
    elif invalid == "message":
        payload["message"] = {"diagnostic": private}
        rule = "string_type"
    else:
        rule = "json_invalid"
    reply.content = ("invalid-json " + private).encode() if invalid == "json" else json.dumps(payload).encode()

    def invoke():
        return reply.client.bind(root) if operation == "bind" else reply.client.resolve(parse_resource_uri(reply.uri))

    with pytest.raises(ValueError) as failure:
        invoke()
    detail = str(failure.value)
    assert "private-native-root" not in detail and private not in detail
    assert f"operation={operation}" in detail and "stage=response" in detail
    assert "error_count=1" in detail and f"rules={rule}" in detail
    assert isinstance(failure.value.__cause__, ValidationError)
    assert any(record.exc_info and record.exc_info[1] is failure.value.__cause__
               and reply.content in record.args for record in caplog.records)
    assert len(reply.requests) == 1
    reply.content = json.dumps({"data": data, "request_id": "req-native-protocol"}).encode()
    assert invoke() == (reply.uri if operation == "bind" else root)
    assert len(reply.requests) == 2
    assert reply.requests[0].method == reply.requests[1].method == ("POST" if operation == "bind" else "GET")
    assert reply.requests[0].url == reply.requests[1].url
    assert reply.requests[0].content == reply.requests[1].content
    assert all(request.headers["X-Local-Token"] == "local-token" for request in reply.requests)


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
async def native_case(
    empty_workspace_scope: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    workspace_id, workspace = empty_workspace_scope
    external = workspace.parent / "未注册 目录"
    shutil.rmtree(external, ignore_errors=True)
    external.mkdir()
    (external / "文件 %.txt").write_text("external", encoding="utf-8")
    (workspace / "same.txt").write_text("workspace", encoding="utf-8")
    config = Mock(spec=ConfigService)
    config.get_workspace_file_default_limit.return_value = 500
    config.get_workspace_preview_max_bytes.return_value = 1024 * 1024
    config.get_workspace_preview_binary_sample_bytes.return_value = 8192
    # Gateway 控制数据也属于本 fixture；重跑不能继承上一用例的目录绑定。
    gateway_root = workspace.parent / "gateway"
    if gateway_root.exists():
        shutil.rmtree(gateway_root)
    state = GatewayStateStore(path=workspace.parent / "gateway/gateway.sqlite")
    registry_path = workspace.parent / "gateway/registry.json"
    registry = GatewayWorkspaceRegistry(storage_path=registry_path, state_store=state)
    registry.upsert(WorkspaceTarget(workspace_id="local-files", name="测试文件 owner",
        root_path=str(workspace), backend_url="http://127.0.0.1:45124", connection_kind="local",
        backend_workspace_id=workspace_id))
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
        bindings = GatewayNativeDirectoryClient(workspace_id=workspace_id,
            connection=GatewayTransportConnection(gateway_url="http://127.0.0.1:45123"),
            local_token_supplier=get_gateway_local_token)
        return WorkspaceService(config_service=config, workspace_root=workspace, native_directories=bindings)
    service = make_service()
    case = SimpleNamespace(workspace=workspace, external=external, state=state,
                           service=service, make_service=make_service, gateway_client=gateway_client,
                           gateway=gateway, registry=registry, registry_path=registry_path)
    await service.start()
    yield case
    await case.service.stop()
    case.service.close()
    gateway_client.close()
    case.registry.close()
    case.state.close()


@pytest.mark.parametrize(
    ("os_name", "system_platform", "expected_option"),
    [
        ("nt", "win32", {"creationflags": 0x200}),
        ("posix", "darwin", {"start_new_session": True}),
        ("posix", "linux", {"start_new_session": True}),
    ],
)
def test_reveal_file_entry_uses_supported_process_isolation_options(
    tmp_path,
    monkeypatch,
    os_name,
    system_platform,
    expected_option,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "same.txt"
    target.write_text("workspace", encoding="utf-8")
    service = WorkspaceService(
        config_service=Mock(spec=ConfigService),
        workspace_root=workspace,
    )
    null_device = object()
    popen = Mock()
    monkeypatch.setattr(
        workspace_service_module,
        "os",
        SimpleNamespace(name=os_name),
    )
    monkeypatch.setattr(
        workspace_service_module,
        "sys",
        SimpleNamespace(platform=system_platform),
    )
    monkeypatch.setattr(
        workspace_service_module,
        "subprocess",
        SimpleNamespace(
            DEVNULL=null_device,
            CREATE_NEW_PROCESS_GROUP=0x200,
            Popen=popen,
        ),
    )
    monkeypatch.setattr(
        workspace_service_module,
        "shutil",
        SimpleNamespace(
            which=lambda name: (
                "/usr/bin/xdg-open" if name == "xdg-open" else None
            ),
        ),
    )

    try:
        displayed = service.reveal_file_entry(path="same.txt", scope="workspace")
        assert service.file_paths.native(displayed) == target.resolve()
        if os_name == "nt":
            expected_command = ["explorer.exe", f"/select,{target}"]
        elif system_platform == "darwin":
            expected_command = ["open", "-R", str(target)]
        else:
            expected_command = ["/usr/bin/xdg-open", str(workspace)]
        popen.assert_called_once_with(
            expected_command,
            stdin=null_device,
            stdout=null_device,
            stderr=null_device,
            **expected_option,
        )
    finally:
        service.close()


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
    external_file_content = await case.service.get_file_content(
        path=str(external_file)
    )
    assert external_file_content.content == "external"
    await case.service.update_file_content(
        path=str(external_file),
        content="right target",
        expected_revision=external_file_content.revision,
    )
    assert external_file.read_text(encoding="utf-8") == "right target"
    assert decoy.read_text(encoding="utf-8") == "wrong target"
    missing = external_file_content.path.replace(
        parse_resource_uri(external_file_content.path).tail_segments[0],
        "019000000000700080000000000000ff",
    )
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


async def test_private_storage_http_rejects_native_uri_and_symlink_aliases(native_case):
    case = native_case
    private = case.workspace / ".boxteam/navigation/private-original.txt"
    private.write_text("private original", encoding="utf-8")
    link = case.external / "private-alias"
    link.symlink_to(private.parent, target_is_directory=True)
    (case.external / "loop").symlink_to("loop")
    (case.external / "broken-link").symlink_to("missing-target")
    workspace_uri = case.service.file_paths.workspace_uri
    native_uri = await asyncio.to_thread(case.service.file_paths.bindings.bind, case.workspace)
    private_uri = await asyncio.to_thread(case.service.file_paths.bindings.bind, private.parent)
    targets = [".boxteam/navigation/private-original.txt", str(private),
        file_uri_child(workspace_uri, ".boxteam", "navigation", private.name),
        file_uri_child(native_uri, ".boxteam", "navigation", private.name),
        file_uri_child(private_uri, private.name), str(link / private.name)]
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(workspace_api.router, prefix="/api/v1")
    app.dependency_overrides[get_workspace_service] = lambda: case.service
    app.dependency_overrides[verify_local_token] = lambda: "authenticated"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend.test") as client:
        for target in targets:
            for suffix in ("content", "raw", "download"):
                response = await client.get(f"/api/v1/workspace/files/{suffix}", params={"path": target})
                assert response.status_code == 403, (target, suffix, response.text)
                assert "private original" not in response.text
                assert str(case.workspace) not in response.text
            saved = await client.put("/api/v1/workspace/files/content", params={"path": target},
                json={"content": "must not publish", "expected_revision": "0" * 64})
            assert saved.status_code == 403, saved.text
            revealed = await client.post("/api/v1/workspace/files/reveal", params={"path": target})
            assert revealed.status_code == 403, revealed.text
            referenced = await client.post("/api/v1/workspace/files/references", json={"paths": [target]})
            assert referenced.status_code == 403, referenced.text
        for directory in (".boxteam", str(link), private_uri):
            response = await client.get("/api/v1/workspace/files", params={"path": directory})
            assert response.status_code == 403, response.text
        workspace_listing = (await client.get("/api/v1/workspace/files")).json()["data"]
        assert ".boxteam" not in {item["name"] for item in workspace_listing["items"]}
        external_listing = (await client.get("/api/v1/workspace/files", params={"path": str(case.external)})).json()["data"]
        assert "private-alias" not in {item["name"] for item in external_listing["items"]}
        assert {item["name"] for item in external_listing["items"] if item["kind"] == "symlink"} == {"loop", "broken-link"}
    assert private.read_text(encoding="utf-8") == "private original"


async def test_private_tree_copy_upload_and_download_reject_before_publication(native_case, monkeypatch):
    case = native_case
    source = case.external / "nested-project"
    (source / ".boxteam").mkdir(parents=True)
    (source / ".boxteam/secret.txt").write_text("private source", encoding="utf-8")
    (source / "public.txt").write_text("public source", encoding="utf-8")
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(workspace_api.router, prefix="/api/v1")
    app.dependency_overrides[get_workspace_service] = lambda: case.service
    app.dependency_overrides[verify_local_token] = lambda: "authenticated"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend.test") as client:
        listing = (await client.get("/api/v1/workspace/files")).json()["data"]
        def intent():
            return WorkspaceFileMutationIntentDTO(client_operation_id=create_prefixed_id("op"),
                client_sequence=1, directory_key=listing["directory_key"],
                expected_directory_revision=listing["directory_revision"]).model_dump()
        requests = [
            ("entries", {"name": ".boxteam", "kind": "directory", "intent": intent()}),
            ("copy", {"source_path": str(source), "source_scope": "filesystem", "intent": intent()}),
            ("paste", {"source_paths": [str(source / "public.txt"), str(source / ".boxteam/secret.txt")], "intent": intent()}),
        ]
        for endpoint, payload in requests:
            response = await client.post(f"/api/v1/workspace/files/{endpoint}", json=payload)
            assert response.status_code == 403, response.text
        uploaded = await client.post("/api/v1/workspace/files/upload",
            data={"relative_paths": "nested-project/.boxteam/injected.txt", "intent": json.dumps(intent())},
            files={"files": ("injected.txt", b"must not publish", "text/plain")})
        assert uploaded.status_code == 403, uploaded.text
        for target in (case.workspace, source):
            response = await client.get("/api/v1/workspace/files/download", params={"path": str(target)})
            assert response.status_code == 403, response.text
        # 普通点号目录及未注册目录的既有文本能力继续由同一 owner 执行。
        ordinary = case.external / ".config"
        ordinary.mkdir()
        (ordinary / "public.txt").write_text("before", encoding="utf-8")
        opened = (await client.get("/api/v1/workspace/files/content", params={"path": str(ordinary / "public.txt")})).json()["data"]
        saved = await client.put("/api/v1/workspace/files/content", params={"path": opened["path"]},
            json={"content": "after", "expected_revision": opened["revision"]})
        assert saved.status_code == 200, saved.text
        assert (ordinary / "public.txt").read_text(encoding="utf-8") == "after"
        assert (await client.get("/api/v1/workspace/files/download", params={"path": str(ordinary)})).status_code == 200
    assert not (case.workspace / source.name).exists()
    assert not (case.workspace / "public.txt").exists()
    assert not list((case.workspace / ".boxteam/navigation/workspace-files-staging").iterdir())
    assert (source / ".boxteam/secret.txt").read_text(encoding="utf-8") == "private source"
    original_scandir = os.scandir
    def public_scan(directory):
        assert ".boxteam" not in Path(directory).parts, "下载已进入私有子树才拒绝"
        return original_scandir(directory)
    with monkeypatch.context() as guarded:
        guarded.setattr(os, "scandir", public_scan)
        with pytest.raises(ForbiddenError):
            case.service.prepare_file_download(path=str(source), scope="filesystem")


async def test_accepted_copy_rejects_source_replaced_by_private_alias(native_case, monkeypatch):
    case = native_case
    private = case.workspace / ".boxteam/private-original.txt"
    private.write_text("private original", encoding="utf-8")
    source = case.external / "文件 %.txt"
    owner = case.service.file_operations
    original_execute = owner._execute
    started, release = threading.Event(), threading.Event()
    def paused(record):
        started.set()
        if not release.wait(5):
            raise TimeoutError("测试没有释放已受理文件操作")
        original_execute(record)
    monkeypatch.setattr(owner, "_execute", paused)
    listing = await case.service.list_files()
    try:
        receipt = await case.service.copy_file_entry(directory_path="", scope="workspace",
            source_path=str(source), source_scope="filesystem", actor="local",
            intent=WorkspaceFileMutationIntentDTO(client_operation_id=create_prefixed_id("op"), client_sequence=1,
                directory_key=listing.directory_key, expected_directory_revision=listing.directory_revision))
        assert await asyncio.to_thread(started.wait, 5)
        source.unlink()
        source.symlink_to(private)
    finally:
        release.set()
    async with asyncio.timeout(5):
        while owner.status([receipt.operation_id], actor="local").operations[0].state in {"queued", "running"}:
            if owner.worker_state == "failed":
                raise AssertionError(owner.worker_error)
            await asyncio.sleep(0.01)
    settled = owner.status([receipt.operation_id], actor="local").operations[0]
    assert settled.state == "rejected" and settled.effect == "none"
    assert owner.worker_state == "running"
    assert not (case.workspace / source.name).exists()
    assert private.read_text(encoding="utf-8") == "private original"
