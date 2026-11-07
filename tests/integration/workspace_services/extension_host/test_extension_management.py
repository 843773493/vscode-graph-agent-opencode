"""真实 Gateway 常驻管理／Backend client／原生 profile；窗口管理 UI 尚不在本切片。"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
import subprocess
import threading
from base64 import b64decode, b64encode
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import JsonValue
from starlette.responses import Response, StreamingResponse
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.api.extension_configuration import router as backend_configuration_router
from app.api.extension_host import router as backend_host_router
from app.container import WorkspaceContainerRegistry
from app.core.backend_identity import load_or_create_backend_id
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.resource_uri import extension_package_uri, resource_display_uri
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.auth import get_gateway_local_token
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import USER_ACCESS_COOKIE_NAME, UserAccessService
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.main import gateway_http_exception_handler
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.extension_download import ExtensionDownloadRequest
from app.gateway.runtime.extension_management import GatewayExtensionManagement
from app.gateway.server.extension_configuration import (
    router as gateway_configuration_router,
)
from app.gateway.server.extension_host import router as gateway_host_router
from app.gateway.server.extension_management import router as gateway_management_router
from app.gateway.server.extension_package import router as gateway_package_router
from app.main import workspace_http_exception_handler
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationEventDTO,
    ExtensionConfigurationViewRequest,
    ExtensionConfigurationWriteOptions,
)
from app.services.infrastructure.extension_host import owner as owner_module
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
    InstalledProfile,
    NativeExtensionPublication,
    NativeFileRead,
    NativeFileStat,
)
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime
from tests.harness.python.run_context import TestRunContext
from tests.support.sse import read_sse_events_until


@pytest.fixture(scope="module")
def management_runtime(request: pytest.FixtureRequest) -> tuple[TestRunContext, Path, NodeHostRuntime]:
    executable, modules = os.getenv("BOXTEAM_TEST_NODE_HOST_EXECUTABLE"), os.getenv("BOXTEAM_TEST_NODE_HOST_MODULES")
    if not executable or not modules:
        pytest.skip("UNMET_PREREQUISITE: 需显式提供固定 Node 及原扩展管理依赖")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    component = context.run_root / "runtime"
    command = ["bun", "--eval", """
import {buildNodeExtensionHost} from './src/clients/workbench/scripts/nodeHost.mjs';
import {symlink} from 'node:fs/promises';
import {resolve,join} from 'node:path';
const [output,modules]=process.argv.slice(-2);
await buildNodeExtensionHost(resolve('src/clients/workbench'),output);
await symlink(resolve(modules),join(output,'node_modules'),'junction');
""", str(component), modules]
    result = subprocess.run(command, cwd=context.project_root, capture_output=True, text=True, timeout=60, check=False)
    (context.artifacts_dir / "build.json").write_text(json.dumps({"command": command, "returncode": result.returncode,
        "stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False, indent=2))
    assert result.returncode == 0, result.stderr
    metadata = json.loads((component / "version.json").read_text())
    return context, component, NodeHostRuntime(Path(executable).resolve(), component / "extensionHostProcess.mjs",
        metadata["commit"], metadata["version"], metadata["nodeVersion"])


@dataclass
class ManagementDomain:
    home: Path
    owner: GatewayExtensionManagement
    application: FastAPI
    registry: GatewayWorkspaceRegistry
    gateway_id: str
    url: str
    headers: dict[str, str]


@dataclass
class ManagementBackends:
    context: TestRunContext
    workspace: Path
    runtime: NodeHostRuntime
    manifest: Path
    token: str
    monkeypatch: pytest.MonkeyPatch
    domains: list[ManagementDomain] = field(default_factory=list)
    registries: list[WorkspaceContainerRegistry] = field(default_factory=list)
    owners: list[GatewayExtensionManagement] = field(default_factory=list)


@pytest.fixture
async def management_backends(
    management_runtime: tuple[TestRunContext, Path, NodeHostRuntime], monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, network_scope: AsyncExitStack,
    serve_extension_application: Callable[[FastAPI], Awaitable[str]],
) -> AsyncIterator[ManagementBackends]:
    context, component, runtime = management_runtime
    context.artifacts_dir.mkdir(parents=True, exist_ok=True)
    home = context.boxteam_home_for_node(request.node.nodeid)
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    manifest = home / "runtime-manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "extension_host": {
        "node_executable": str(runtime.executable), "component_root": str(component)}}))
    monkeypatch.setenv("BOXTEAM_RUNTIME_MANIFEST", str(manifest))
    token = get_gateway_local_token()
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", token)
    workspace = context.workspace_root / context.run_root.name / uuid4().hex
    workspace.mkdir(parents=True)
    case = ManagementBackends(context, workspace, runtime, manifest, token, monkeypatch)
    client = await network_scope.enter_async_context(httpx.AsyncClient(timeout=10, trust_env=False))
    try:
        for name in ("a", "b"):
            domain_home = home / name
            backend_id = load_or_create_backend_id(domain_home)
            owner = GatewayExtensionManagement(backend_id=backend_id, runtime=runtime,
                directory=domain_home / "state/backend/extension-management" / backend_id,
                environment={**os.environ, "BOXTEAM_PROJECT_ROOT": str(context.project_root)},
                log_dir=domain_home / "state/logs",
                logging_startup={"revision": "extension-management-fixture", "level": "info", "pretty": False})
            case.owners.append(owner)
            await owner.start()
            gateway_id = "gateway_" + uuid4().hex
            registry = GatewayWorkspaceRegistry(storage_path=domain_home / "workspaces.json", gateway_id=gateway_id)
            network_scope.callback(registry.close)
            state = GatewayStateStore(path=domain_home / "gateway.sqlite")
            network_scope.callback(state.close)
            users = UserAccessService(state=state)
            users.create_user(display_name="扩展库存", user_id="inventory")
            access = users.acquire_user(user_id="inventory", client_label="real-network")
            application = FastAPI()
            application.add_middleware(TraceMiddleware)
            application.exception_handler(HTTPException)(gateway_http_exception_handler)
            application.include_router(gateway_management_router)
            application.include_router(gateway_host_router)
            application.include_router(gateway_package_router)
            application.state.extension_management = owner
            application.state.registry = registry
            application.state.user_access_service = users
            application.state.federation_policy_store = FederationPolicyStore()
            application.state.http_client = client
            url = await serve_extension_application(application)
            # 同进程装具共用原本机认证凭据；安装 Home、稳定身份和真实 child 各自独立。
            case.domains.append(ManagementDomain(domain_home, owner, application, registry, gateway_id, url,
                {"X-Local-Token": token, "Cookie": f"{USER_ACCESS_COOKIE_NAME}={access.access_session_id}"}))
        yield case
    finally:
        failures: list[BaseException] = []
        for registry in case.registries:
            try:
                await registry.close_extension_hosts()
                registry.close()
            except BaseException as error:  # noqa: BLE001 - 继续关闭其他真实 Backend 和 Gateway。
                failures.append(error)
        for owner in case.owners:
            instance = owner.instance
            process = owner.process
            try:
                await owner.close()
                assert owner.drained
                if process is not None:
                    assert not process.is_alive()
            except BaseException as error:  # noqa: BLE001 - 每个原 child 均保留实际退出收据。
                failures.append(error)
            finally:
                process_name = "unavailable" if instance is None else instance.runtime_instance_id.replace(":", "-")
                (context.artifacts_dir / f"process-{owner.backend_id}-{process_name}.json").write_text(
                    json.dumps({"backend_id": owner.backend_id,
                        "instance": None if instance is None else instance.model_dump(mode="json"),
                        "process_id": None if instance is None else instance.process_id,
                        "exited": None if process is None else not process.is_alive()}))
        if failures:
            raise BaseExceptionGroup("真实扩展管理装具回收失败", failures)


def _backend(workspace: Path, case: ManagementBackends, *, domain: int = 0,
             roots: tuple[Path, ...] = ()) -> WorkspaceContainerRegistry:
    workspace.mkdir(exist_ok=True)
    selected = case.domains[domain]
    with case.monkeypatch.context() as environment:
        environment.setenv("BOXTEAM_HOME", str(selected.home))
        environment.setenv("BOXTEAM_GATEWAY_URL", selected.url)
        environment.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", case.token)
        environment.setenv("BOXTEAM_RUNTIME_MANIFEST", str(case.manifest))
        registry = WorkspaceContainerRegistry(backend_id=load_or_create_backend_id(selected.home),
            mounted_workspaces=MountedWorkspaceRegistry([
                MountedWorkspace(load_or_create_workspace_id(root), root) for root in roots or (workspace,)]))
    case.registries.append(registry)
    return registry


async def _restart_gateway_management(case: ManagementBackends, *, domain: int = 0) -> None:
    selected = case.domains[domain]
    await selected.owner.close()
    owner = GatewayExtensionManagement(backend_id=load_or_create_backend_id(selected.home), runtime=case.runtime,
        directory=selected.owner.directory,
        environment={**os.environ, "BOXTEAM_PROJECT_ROOT": str(case.context.project_root)}, log_dir=selected.home / "state/logs",
        logging_startup={"revision": "extension-management-fixture", "level": "info", "pretty": False})
    case.owners.append(owner)
    await owner.start()
    selected.owner = owner
    selected.application.state.extension_management = owner


@pytest.fixture
def vsix_factory():
    def create(directory: Path, version: str, content: str, *, engine: str = "^1.0.0",
               manifest_overrides: dict[str, object] | None = None, omit: tuple[str, ...] = ()) -> Path:
        vsix = directory / f"probe-{version}-{uuid4()}.vsix"
        manifest: dict[str, object] = {"name": "target-inventory", "publisher": "boxteam-probe", "version": version,
            "engines": {"vscode": engine}, "main": "./extension.js", "extensionKind": ["workspace"],
            "activationEvents": ["onCommand:boxteam.inventory"],
            "contributes": {"commands": [{"command": "boxteam.inventory", "title": "库存测试"}]}}
        manifest.update(manifest_overrides or {})
        for key in omit:
            manifest.pop(key)
        with ZipFile(vsix, "w") as archive:
            archive.writestr("extension/package.json", json.dumps(manifest))
            archive.writestr("extension/extension.js", f"exports.activate = () => {json.dumps(content)};\n")
        return vsix
    return create


@pytest.fixture
def installation_snapshot() -> Callable[[Path], dict[str, str]]:
    def snapshot(directory: Path) -> dict[str, str]:
        # 只核原安装域，不把管理日志或 scanner 的派生缓存误当制品；目录也必须保持。
        return {path.relative_to(directory).as_posix(): sha256(path.read_bytes()).hexdigest() if path.is_file() else "directory"
                for path in sorted(directory.rglob("*"))}
    return snapshot


@pytest.mark.parametrize("retained", [False, True], ids=["current-version", "retained-version"])
async def test_native_vsix_keep_existing_reuses_published_version(
    management_backends, vsix_factory, installation_snapshot, retained: bool,
) -> None:
    case = management_backends
    context, workspace = case.context, case.workspace
    registry = _backend(workspace / "target", case)
    management = registry.extension_hosts.management
    published = await management.install(vsix_factory(workspace, "1.0.0", "published-v1",
        manifest_overrides={"displayName": "已发布的 V1"}))
    assert published.installed.reused is False
    installation = case.domains[0].owner.directory / "extensions"
    profile = installation / "extensions.json"
    original = installation / json.loads(profile.read_text())[0]["relativeLocation"]
    original_snapshot = installation_snapshot(original)
    original_script = (original / "extension.js").read_bytes()
    newer = None
    newer_snapshot = None
    if retained:
        upgraded = await management.install(vsix_factory(workspace, "2.0.0", "published-v2"))
        assert upgraded.installed.reused is False
        newer = installation / json.loads(profile.read_text())[0]["relativeLocation"]
        newer_snapshot = installation_snapshot(newer)
        assert json.loads((installation / ".obsolete").read_text())[original.name] is True

    # 同 key 的候选仍须先通过原 manifest 预检，不能因为可以复用就忽略无效输入。
    before = installation_snapshot(installation)
    profile_bytes = profile.read_bytes()
    with pytest.raises(ExtensionManagementError, match="property `main` can be omitted"):
        await management.install(vsix_factory(workspace, "1.0.0", "invalid-candidate", manifest_overrides={"main": 42}))
    assert installation_snapshot(installation) == before
    assert profile.read_bytes() == profile_bytes

    candidate = vsix_factory(workspace, "1.0.0", "candidate-must-not-replace-published-v1",
        manifest_overrides={"displayName": "候选的另一份 V1"})
    with ZipFile(candidate) as archive:
        candidate_script = archive.read("extension/extension.js")
    assert candidate_script != original_script
    reused = await management.install(candidate)
    assert reused.installed == published.installed.model_copy(update={"reused": True})
    assert len(reused.inventory.extensions) == 1
    extension = reused.inventory.extensions[0]
    assert extension.is_valid and extension.description["version"] == "1.0.0"
    assert extension.description["displayName"] == "已发布的 V1"
    assert extension.description["extensionLocation"] == published.inventory.extensions[0].description["extensionLocation"]
    assert json.loads(profile.read_text())[0]["relativeLocation"] == original.name
    assert installation_snapshot(original) == original_snapshot
    assert (original / "extension.js").read_bytes() != candidate_script
    if newer is not None:
        assert installation_snapshot(newer) == newer_snapshot
        obsolete = json.loads((installation / ".obsolete").read_text())
        assert original.name not in obsolete and obsolete[newer.name] is True
    assert await management.inventory() == reused.inventory
    case.domains[0].owner.assert_healthy()
    assert registry.extension_hosts.active == ()
    (context.artifacts_dir / f"vsix-reuse-{'retained' if retained else 'current'}.json").write_text(json.dumps({
        "published": published.model_dump(), "result": reused.model_dump(),
        "original_snapshot": original_snapshot, "after_snapshot": installation_snapshot(original),
        "published_script_sha256": sha256(original_script).hexdigest(),
        "candidate_script_sha256": sha256(candidate_script).hexdigest(),
    }, ensure_ascii=False, indent=2))


async def test_web_publication_freezes_native_descriptors_and_reads_without_further_node_calls(
    management_backends: ManagementBackends, vsix_factory,
    network_scope: AsyncExitStack, serve_extension_application: Callable[[FastAPI], Awaitable[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = management_backends
    registry = _backend(case.workspace / "web-publication", case)
    domain = case.domains[0]
    management = registry.extension_hosts.management
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.exception_handler(HTTPException)(workspace_http_exception_handler)
    application.include_router(backend_host_router)
    application.state.workspace_container_registry = registry
    backend_url = await serve_extension_application(application)
    [mount] = registry.mounted_workspaces.mounts
    domain.registry.upsert(WorkspaceTarget(gateway_id=domain.gateway_id, workspace_id=mount.workspace_id,
        name="冻结发布", root_path=str(mount.workspace_root), backend_url=backend_url,
        connection_kind="local", backend_id=registry.extension_hosts.backend_id), activate=False)
    client = await network_scope.enter_async_context(httpx.AsyncClient(timeout=10, trust_env=False, headers=domain.headers))
    node_calls: list[str] = []

    async def observe_node(request: httpx.Request) -> None:
        node_calls.append(request.url.path)

    assert domain.owner._client is not None
    domain.owner._client.event_hooks["request"].append(observe_node)

    async def install(version: str):
        source = vsix_factory(case.workspace, version, version, manifest_overrides={"browser": "./extension.js"})
        with ZipFile(source, "a") as archive:
            archive.writestr("extension/dist/helper.js", f"export const version = '{version}';")
            archive.writestr("extension/style.css", 'body { background: url("./images/icon.svg") }')
            archive.writestr("extension/images/icon.svg", f"<svg><title>{version}</title></svg>")
        return await management.install(source)

    installed = await install("1.0.0")
    profile_id = installed.installed.profile_id
    first_key = installed.inventory.extensions[0].extension_key
    extension_id = installed.installed.id
    ws_url = domain.url.replace("http://", "ws://") + "/api/gateway/extension-web-hosts/channel"

    async def open_view(view: str, key: str):
        channel = await connect(ws_url, additional_headers=domain.headers, compression=None)
        await channel.send(json.dumps({"protocol_version": 1, "gateway_id": domain.gateway_id,
            "backend_id": registry.extension_hosts.backend_id, "view_id": view, "workspace_ids": [],
            "profile_id": profile_id, "extension_keys": [key]}))
        return channel, json.loads(await channel.recv())

    def content_url(binding: dict[str, object], key: str) -> str:
        return (f"{domain.url}/api/gateway/extension-packages/content/{domain.gateway_id}/"
            f"{registry.extension_hosts.backend_id}/{profile_id}/{binding['view_id']}/{binding['generation']}/"
            f"{key}/{extension_id}/extension.js")

    async def check_reads(binding: dict[str, object], key: str, version: str) -> None:
        before = len(node_calls)
        entry_url = content_url(binding, key)
        for relative, content in (("extension.js", f'exports.activate = () => "{version}";\n'),
                                  ("dist/helper.js", f"export const version = '{version}';"),
                                  ("style.css", 'body { background: url("./images/icon.svg") }'),
                                  ("images/icon.svg", f"<svg><title>{version}</title></svg>")):
            url = urljoin(entry_url, relative)
            if relative == "dist/helper.js":
                response = await client.send(httpx.Request("GET", url, headers={"Cookie": domain.headers["Cookie"],
                    "Sec-Fetch-Site": "same-origin", "X-Request-ID": f"web-frozen-{version}-{relative}"}))
            else:
                response = await client.get(url, headers={"X-Request-ID": f"web-frozen-{version}-{relative}"})
            assert response.status_code == 200, response.text
            assert response.content == content.encode() and response.headers["Cache-Control"] == "no-store"
        resource = extension_package_uri(gateway_id=domain.gateway_id, backend_id=registry.extension_hosts.backend_id,
            profile_id=profile_id, extension_id=extension_id, relative_segments=("extension.js",))
        stat = await client.post(f"{domain.url}/api/gateway/extension-packages/stat", json={
            "protocol_version": 1, "gateway_id": domain.gateway_id, "backend_id": registry.extension_hosts.backend_id,
            "resource_uri": resource, "extension_key": key, "view_id": binding["view_id"], "generation": binding["generation"]})
        assert stat.status_code == 200 and stat.json()["data"]["size"] == len(f'exports.activate = () => "{version}";\n')
        assert len(node_calls) == before, "冻结 Web stat/read 不得再调用 Node 发布或库存接口"

    async def rejected_open(key: str) -> None:
        async with connect(ws_url, additional_headers=domain.headers, compression=None) as channel:
            await channel.send(json.dumps({"protocol_version": 1, "gateway_id": domain.gateway_id,
                "backend_id": registry.extension_hosts.backend_id, "view_id": "rejected-new-view", "workspace_ids": [],
                "profile_id": profile_id, "extension_keys": [key]}))
            with pytest.raises(ConnectionClosed):
                await channel.recv()

    async def settled(binding: dict[str, object]) -> None:
        async with asyncio.timeout(5):
            while binding["generation"] in registry.extension_hosts._sessions:
                await asyncio.sleep(0.01)

    first, binding_a = await open_view("web-publication-a", first_key)
    try:
        assert node_calls.count("/management") == 2  # 一次安装与一次真实 Web scanner 发布。
        await check_reads(binding_a, first_key, "1.0.0")
        upgraded = await install("2.0.0")
        second_key = upgraded.inventory.extensions[0].extension_key
        await rejected_open(first_key)
        second, binding_b = await open_view("web-publication-b", second_key)
        try:
            assert binding_a["generation"] != binding_b["generation"]
            await management.uninstall(profile_id=profile_id, extension_id=extension_id, version="2.0.0")
            await rejected_open(second_key)
            await check_reads(binding_a, first_key, "1.0.0")
            await check_reads(binding_b, second_key, "2.0.0")
            before = len(node_calls)
            foreign_id = content_url(binding_a, first_key).replace(f"/{extension_id}/", "/publisher.other/")
            assert (await client.get(foreign_id)).status_code == 409
            foreign_key = content_url(binding_a, first_key).replace(f"/{first_key}/", f"/{second_key}/")
            assert (await client.get(foreign_key)).status_code == 409
            assert len(node_calls) == before
        finally:
            await second.close()
        await settled(binding_b)
        assert (await client.get(content_url(binding_b, second_key))).status_code == 409
        await check_reads(binding_a, first_key, "1.0.0")

        started, release = threading.Event(), threading.Event()
        original_read = owner_module.publication_file

        def blocked_read(publication: NativeExtensionPublication, *, extension_key: str, relative_segments: object,
                         file_operation: Literal["stat", "read"]) -> NativeFileRead | NativeFileStat:
            started.set()
            assert release.wait(5), "测试必须释放已开始的 Python 文件读取"
            return original_read(publication, extension_key=extension_key, relative_segments=relative_segments, file_operation=file_operation)

        monkeypatch.setattr(owner_module, "publication_file", blocked_read)
        reading = asyncio.create_task(client.get(content_url(binding_a, first_key)))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            users = domain.application.state.user_access_service
            access = users.resolve_cookie(domain.headers["Cookie"].split("=", 1)[1])
            assert access is not None
            users.release(access)
            session = registry.extension_hosts._sessions[binding_a["generation"]]
            assert isinstance(session, owner_module._WebHostSession)
            await asyncio.wait_for(session.stopping.wait(), 3)
            assert session.pending_reads == 1 and not session.closed
            release.set()
            response = await reading
            assert response.status_code == 403 and b"exports.activate" not in response.content
            await settled(binding_a)
            assert (await client.get(content_url(binding_a, first_key))).status_code == 401
        finally:
            release.set()
            await asyncio.gather(reading, return_exceptions=True)
        (case.context.artifacts_dir / "web-frozen-publication.json").write_text(json.dumps({
            "binding_a": binding_a, "binding_b": binding_b, "node_calls": node_calls,
            "frozen_reads_without_node": True, "update_uninstall_old_reads": True,
            "new_admission_rejected": True, "relative_raw_resources": True,
            "revoked_late_body_rejected": True}, ensure_ascii=False, indent=2) + "\n")
    finally:
        await first.close()


@pytest.mark.parametrize("root_count", [0, 2], ids=["empty-view", "multi-root-view"])
@pytest.mark.parametrize("host_bound", [False, True], ids=["standalone-settings", "web-host"])
async def test_web_context_routes_configuration_to_target_python_without_node_runtime(
    management_backends: ManagementBackends, vsix_factory, root_count: int, host_bound: bool,
    network_scope: AsyncExitStack, serve_extension_application: Callable[[FastAPI], Awaitable[str]],
) -> None:
    case = management_backends
    domain = case.domains[0]
    # 工作区后端继续持有合法 mounts；浏览器视图可不挂接它们，也不需要 Node runtime。
    roots = tuple(case.workspace / f"web-root-{index}" for index in range(root_count))
    for root in roots:
        root.mkdir()
    registry = _backend(case.workspace / "web-configuration", case, roots=roots)
    owner = registry.extension_hosts
    owner.runtime = None
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.exception_handler(HTTPException)(workspace_http_exception_handler)
    application.include_router(backend_host_router)
    application.include_router(backend_configuration_router)
    application.state.workspace_container_registry = registry
    backend_url = await serve_extension_application(application)
    domain.application.include_router(gateway_configuration_router)
    domain.application.state.streaming_http_client = await network_scope.enter_async_context(
        httpx.AsyncClient(timeout=None, trust_env=False))
    for mount in registry.mounted_workspaces.mounts:
        domain.registry.upsert(WorkspaceTarget(gateway_id=domain.gateway_id, workspace_id=mount.workspace_id,
            name=mount.workspace_root.name, root_path=str(mount.workspace_root), backend_url=backend_url,
            connection_kind="local", backend_id=owner.backend_id), activate=False)
    payload = {"protocol_version": 1, "gateway_id": domain.gateway_id, "backend_id": owner.backend_id,
        "view_id": "web-configuration", "workspace_ids": list(registry.mounted_workspaces.workspace_ids) if root_count else []}
    if host_bound:
        installed = await owner.management.install(vsix_factory(case.workspace, "1.0.0", "web-config",
            manifest_overrides={"browser": "./extension.js"}))
        [entry] = installed.inventory.extensions
        payload.update(profile_id=installed.inventory.profile_id, extension_keys=[entry.extension_key])
    node_calls: list[str] = []

    async def observe(request: httpx.Request) -> None:
        node_calls.append(request.url.path)

    assert domain.owner._client is not None
    domain.owner._client.event_hooks["request"].append(observe)
    client = await network_scope.enter_async_context(httpx.AsyncClient(timeout=10, trust_env=False, headers=domain.headers))
    endpoint = domain.url.replace("http://", "ws://") + "/api/gateway/extension-web-hosts/channel"
    async with AsyncExitStack() as channels:
        if host_bound:
            channel = await channels.enter_async_context(connect(endpoint, additional_headers=domain.headers, compression=None))
            await channel.send(json.dumps(payload))
            binding = json.loads(await channel.recv())
        else:
            prepared = await client.post(domain.url + "/api/gateway/extension-configurations/prepare", json=payload,
                headers={"X-Request-ID": "settings-prepare"})
            assert prepared.status_code == 200, prepared.text
            assert prepared.headers["X-Request-ID"] == prepared.json()["request_id"] == "settings-prepare"
            binding = prepared.json()["data"]
            assert owner.active == () and "generation" not in binding
        context = binding["context"]
        assert set(context) == {"os", "remote_settings", "workspace"}
        assert [folder["workspace_id"] for folder in context["workspace"]["folders"]] == payload["workspace_ids"]
        assert (context["workspace"]["configuration"] is not None) == bool(root_count)
        assert node_calls == ["/management"]
        request = {name: binding[name] for name in ("protocol_version", "gateway_id", "backend_id", "view_id", "workspace_ids")}
        request.update({"generation": binding["generation"]} if host_bound else {"profile_id": binding["profile_id"]})
        settings_uri = context["remote_settings"]["resource_uri"]
        resources = [context["remote_settings"]]
        if context["workspace"]["configuration"] is not None:
            resources.append(context["workspace"]["configuration"])
        receipts = []
        for resource in resources:
            if resource is resources[0]:
                content = b'/* target Python */\n{"probe.setting": 17}\n'
            else:
                folders = [{"uri": folder["native_uri"], "name": folder["name"]} for folder in context["workspace"]["folders"]]
                content = ("/* target view */\n" + json.dumps({"folders": folders, "settings": {"probe.setting": 18}})).encode()
            encoded = b64encode(content).decode()
            saved = await client.post(domain.url + "/api/gateway/extension-configurations/write",
                json={**request, "resource_uri": resource["resource_uri"], "content_base64": encoded,
                    "options": {"create": True, "overwrite": True, "unlock": False, "atomic": False, "append": False}},
                headers={"X-Request-ID": "web-configuration-save"})
            assert saved.status_code == 200, saved.text
            assert saved.headers["X-Request-ID"] == saved.json()["request_id"] == "web-configuration-save"
            read = await client.post(domain.url + "/api/gateway/extension-configurations/read",
                json={**request, "resource_uri": resource["resource_uri"]})
            assert read.status_code == 200 and read.json()["data"]["content_base64"] == encoded
            native = Path(url2pathname(urlsplit(resource["native_uri"]).path))
            assert native.read_bytes() == content
            receipts.append(saved.json())
        ready = asyncio.Event()

        def changed(event: dict[str, object]) -> bool:
            value = ExtensionConfigurationEventDTO.model_validate(event)
            assert value.error is None
            if value.overflow:
                ready.set()
            return any(change.resource_uri == settings_uri for change in value.changes)

        async with client.stream("POST", domain.url + "/api/gateway/extension-configurations/events", json=request) as response:
            assert response.status_code == 200, await response.aread()
            watching = asyncio.create_task(read_sse_events_until(response, changed, timeout_seconds=5))
            try:
                await asyncio.wait_for(ready.wait(), 3)
                native_settings = Path(url2pathname(urlsplit(context["remote_settings"]["native_uri"]).path))
                native_settings.write_text('/* external */\n{"probe.setting": 23}\n')
                events = await watching
                assert any(event.get("changes") for event in events)
            finally:
                watching.cancel()
                await asyncio.gather(watching, return_exceptions=True)
        assert node_calls == ["/management"], "配置 I/O 和监听不得重新请求 Node"
        assert not (owner._home / "state/backend/extension-hosts").exists()
    if not host_bound:
        owner.close_admission()
    rejected = await client.post(domain.url + "/api/gateway/extension-configurations/read",
        json={**request, "resource_uri": settings_uri})
    assert rejected.status_code == 409 and "content_base64" not in rejected.text
    (case.context.artifacts_dir / f"web-context-{root_count}-{host_bound}.json").write_text(json.dumps({
        "binding": binding, "writes": receipts, "events": events, "node_calls": node_calls,
        "after_close_status": rejected.status_code}, ensure_ascii=False, indent=2) + "\n")


async def test_native_published_extension_files_are_read_by_exact_retained_key(
    management_backends, vsix_factory, network_scope: AsyncExitStack,
) -> None:
    case = management_backends
    context, workspace = case.context, case.workspace
    registry = _backend(workspace / "target", case)
    management = registry.extension_hosts.management
    original = await management.install(vsix_factory(workspace, "1.0.0", "published-v1"))
    profile_id = original.installed.profile_id
    key = "boxteam-probe.target-inventory-1.0.0"
    root = case.domains[0].owner.directory / "extensions"
    old_directory = root / key
    script = b'exports.activate = () => "published-v1";\n'
    assert (old_directory / "extension.js").read_bytes() == script

    stat = await management.extension_file_stat(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key,
                                                relative_segments=["extension.js"], current_profile_member=False)
    root_stat = await management.extension_file_stat(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key,
        relative_segments=[], current_profile_member=False)
    assert root_stat.type & 2  # FileType.Directory：包根作为原生目录可 stat。
    read = await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key,
                                                relative_segments=["extension.js"], current_profile_member=False)
    assert stat.size == len(script)
    assert b64decode(read.content_base64) == script
    assert any(extension.extension_key == key for extension in original.inventory.extensions)
    current_read = await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key,
        relative_segments=["extension.js"], current_profile_member=True)
    assert b64decode(current_read.content_base64) == script
    (old_directory / "inside-link").symlink_to(old_directory / "extension.js")
    assert b64decode((await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key,
                                                           relative_segments=["inside-link"], current_profile_member=False)).content_base64) == script

    await management.install(vsix_factory(workspace, "1.1.0-beta.1", "published-prerelease"))
    prerelease_key = "boxteam-probe.target-inventory-1.1.0-beta.1"
    assert b64decode((await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=prerelease_key,
                                                           relative_segments=["extension.js"], current_profile_member=False)).content_base64) == b'exports.activate = () => "published-prerelease";\n'

    # 升级并逻辑卸载当前版本后，仍可按原安装目录读取旧版本。
    await management.install(vsix_factory(workspace, "2.0.0", "published-v2"))
    current_v2_key = "boxteam-probe.target-inventory-2.0.0"
    assert b64decode((await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=current_v2_key,
        relative_segments=["extension.js"], current_profile_member=True)).content_base64) == b'exports.activate = () => "published-v2";\n'
    with pytest.raises(ExtensionManagementError) as stale_current_key:
        await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key,
            relative_segments=["extension.js"], current_profile_member=True)
    assert stale_current_key.value.code == "InstalledExtensionNotFound"
    domain = case.domains[0]
    client = await network_scope.enter_async_context(httpx.AsyncClient(timeout=10, trust_env=False))
    request_id = "retained-extension-read-" + uuid4().hex
    resource = extension_package_uri(gateway_id=domain.gateway_id, backend_id=registry.extension_hosts.backend_id,
        profile_id=profile_id, extension_id="boxteam-probe.target-inventory", relative_segments=("extension.js",))
    response = await client.post(f"{domain.url}/api/gateway/extension-packages/read",
        headers={**domain.headers, "X-Request-ID": request_id},
        json={"protocol_version": 1, "gateway_id": domain.gateway_id, "backend_id": registry.extension_hosts.backend_id,
              "resource_uri": resource, "extension_key": current_v2_key})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
    assert response.json()["data"]["resource_uri"] == resource
    assert response.json()["data"]["extension_key"] == current_v2_key
    assert b64decode(response.json()["data"]["content_base64"]) == b'exports.activate = () => "published-v2";\n'

    await management.uninstall(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", version="2.0.0")
    with pytest.raises(ExtensionManagementError) as uninstalled_current_key:
        await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=current_v2_key,
            relative_segments=["extension.js"], current_profile_member=True)
    assert uninstalled_current_key.value.code == "InstalledExtensionNotFound"
    assert b64decode((await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key,
                                                           relative_segments=["extension.js"], current_profile_member=False)).content_base64) == script

    with pytest.raises(ExtensionManagementError) as wrong_profile:
        await management.extension_file_read(profile_id="wrong-profile", extension_id="boxteam-probe.target-inventory", extension_key=key,
                                             relative_segments=["extension.js"], current_profile_member=False)
    assert wrong_profile.value.code == "Invalid"
    with pytest.raises(ExtensionManagementError) as traversal:
        await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key,
                                             relative_segments=["..", "outside.txt"], current_profile_member=False)
    assert traversal.value.code == "NoPermissions"
    with pytest.raises(ExtensionManagementError) as not_directory:
        await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key,
                                             relative_segments=["extension.js", "nested.js"], current_profile_member=False)
    assert not_directory.value.code == "EntryNotADirectory"
    with pytest.raises(ExtensionManagementError) as missing_key:
        await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key.replace("1.0.0", "3.0.0"),
                                             relative_segments=["extension.js"], current_profile_member=False)
    assert missing_key.value.code == "EntryNotFound"
    with pytest.raises(ExtensionManagementError) as mismatched_id:
        await management.extension_file_read(profile_id=profile_id, extension_id="other.publisher",
            extension_key=key, relative_segments=["extension.js"], current_profile_member=False)
    assert mismatched_id.value.code == "InstalledExtensionNotFound"
    with pytest.raises(ExtensionManagementError) as key_traversal:
        await management.extension_file_stat(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key="../outside-1.0.0",
                                             relative_segments=[], current_profile_member=False)
    assert key_traversal.value.code == "Invalid"

    outside = workspace / "outside.txt"
    outside.write_text("must not be exposed", encoding="utf-8")
    try:
        (old_directory / "outside-link").symlink_to(outside)
    except (NotImplementedError, OSError) as error:
        if os.name == "nt":
            pytest.skip(f"当前 Windows 目标不允许创建测试 symlink: {error}")
        raise
    with pytest.raises(ExtensionManagementError) as symlink:
        await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key,
                                             relative_segments=["outside-link"], current_profile_member=False)
    assert symlink.value.code == "NoPermissions"

    external_package = workspace / "external-package"
    shutil.copytree(old_directory, external_package, symlinks=True)
    saved_package = root / (key + ".saved")
    old_directory.rename(saved_package)
    try:
        old_directory.symlink_to(external_package, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        if os.name == "nt":
            saved_package.rename(old_directory)
            pytest.skip(f"当前 Windows 目标不允许创建目录 symlink: {error}")
        raise
    with pytest.raises(ExtensionManagementError) as package_symlink:
        await management.extension_file_stat(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key, relative_segments=[],
                                             current_profile_member=False)
    assert package_symlink.value.code == "NoPermissions"
    old_directory.unlink()
    saved_package.rename(old_directory)

    large_file = old_directory / "large.bin"
    large_file.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(ExtensionManagementError) as oversized:
        await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory",
            extension_key=key, relative_segments=["large.bin"], current_profile_member=False)
    assert oversized.value.code == "EntryTooLarge"
    (old_directory / "package.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ExtensionManagementError) as invalid_package:
        await management.extension_file_read(profile_id=profile_id, extension_id="boxteam-probe.target-inventory", extension_key=key,
                                             relative_segments=["extension.js"], current_profile_member=False)
    assert invalid_package.value.code == "InstalledExtensionNotFound"
    case.domains[0].owner.assert_healthy()
    assert registry.extension_hosts.active == ()
    (context.artifacts_dir / "published-extension-read.json").write_text(json.dumps({
        "profile_id": profile_id, "extension_key": key, "stat": stat.model_dump(),
        "read_size": len(b64decode(read.content_base64)), "old_version_read_after_upgrade": True,
        "current_package_http_read_ms": response.elapsed.total_seconds() * 1000,
    }, ensure_ascii=False, indent=2))


@pytest.mark.parametrize("retained", [False, True], ids=["current-version", "retained-version"])
async def test_native_vsix_keep_existing_plans_dependencies_from_published_manifest(
    management_backends, vsix_factory, installation_snapshot, serve_extension_application,
    monkeypatch: pytest.MonkeyPatch, retained: bool,
) -> None:
    case = management_backends
    context, workspace = case.context, case.workspace
    application = FastAPI()
    requests: list[str] = []

    @application.post("/gallery/extensionquery")
    async def query(request: Request):
        # 仅 Marketplace 是外部服务替身；原查询、依赖规划、安装和 profile 都真实运行。
        requests.append((await request.body()).decode())
        return {"results": [{"extensions": [], "resultMetadata": []}]}

    url = await serve_extension_application(application)
    product = json.loads((context.project_root / "src/clients/workbench/upstream/product.json").read_text())
    metadata = json.loads((context.run_root / "runtime/version.json").read_text())
    product.update({"version": metadata["version"], "commit": metadata["commit"], "quality": "stable",
        "extensionsGallery": {"serviceUrl": f"{url}/gallery"}})
    preload = context.artifacts_dir / "gallery-product.mjs"
    preload.write_text("globalThis._VSCODE_PRODUCT_JSON = " + json.dumps(product) + ";\n"
        + "globalThis._VSCODE_PACKAGE_JSON = " + json.dumps({"version": metadata["version"]}) + ";\n")
    monkeypatch.setenv("NODE_OPTIONS", f"{os.getenv('NODE_OPTIONS', '')} --import={preload.as_uri()}".strip())
    await _restart_gateway_management(case)
    registry = _backend(workspace / "target", case)
    management = registry.extension_hosts.management
    published_dependency, published_pack = "boxteam-probe.published-dependency", "boxteam-probe.published-pack"
    candidate_dependency, candidate_pack = "boxteam-probe.candidate-dependency", "boxteam-probe.candidate-pack"
    published = await management.install(vsix_factory(workspace, "1.0.0", "published-v1", manifest_overrides={
        "extensionDependencies": [published_dependency], "extensionPack": [published_pack]}))
    assert any(json.dumps(published_dependency) in body and json.dumps(published_pack) in body for body in requests)
    installation = case.domains[0].owner.directory / "extensions"
    profile = installation / "extensions.json"
    original = installation / json.loads(profile.read_text())[0]["relativeLocation"]
    original_snapshot = installation_snapshot(original)
    if retained:
        await management.install(vsix_factory(workspace, "2.0.0", "current-v2"))
    requests.clear()
    reused = await management.install(vsix_factory(workspace, "1.0.0", "candidate-v1", manifest_overrides={
        "extensionDependencies": [candidate_dependency], "extensionPack": [candidate_pack]}))
    assert reused.installed.reused and reused.installed.version == "1.0.0"
    assert any(json.dumps(published_dependency) in body for body in requests)
    assert any(json.dumps(published_pack) in body for body in requests) is retained
    assert all(json.dumps(candidate_dependency) not in body and json.dumps(candidate_pack) not in body for body in requests)
    assert reused.inventory == published.inventory
    assert installation_snapshot(original) == original_snapshot
    assert await management.inventory() == reused.inventory
    (context.artifacts_dir / "published-manifest-dependency-queries.json").write_text(json.dumps({
        "retained": retained, "queries": requests, "result": reused.model_dump(),
    }, ensure_ascii=False, indent=2))


@pytest.mark.parametrize(("primary_fails", "signature_enabled", "body_disconnect"), [
    (False, False, False), (True, False, False), (False, True, False), (False, False, True),
], ids=["primary", "fallback", "signature-rejected", "body-disconnect"])
async def test_gateway_install_downloads_dependency_through_its_target_backend(
    management_backends, vsix_factory, installation_snapshot, network_scope: AsyncExitStack,
    serve_extension_application: Callable[[FastAPI], Awaitable[str]], monkeypatch: pytest.MonkeyPatch,
    primary_fails: bool, signature_enabled: bool, body_disconnect: bool,
) -> None:
    case = management_backends
    context = case.context
    dependency_id = "boxteam-probe.python-dependency"
    dependency_vsix = vsix_factory(case.workspace, "1.0.0", "dependency", manifest_overrides={
        "publisher": "boxteam-probe", "name": "python-dependency"})
    compressed_dependency = gzip.compress(dependency_vsix.read_bytes())
    gallery = FastAPI()
    gallery_requests: list[tuple[str, int, str | None]] = []

    @gallery.post("/gallery/extensionquery")
    async def query(request: Request):
        gallery_requests.append(("/gallery/extensionquery", 200, None))
        version = {
            "version": "1.0.0", "lastUpdated": "2026-01-01T00:00:00Z",
            "assetUri": f"{gallery_url}/assets/primary/python-dependency/1.0.0",
            "fallbackAssetUri": f"{gallery_url}/assets/fallback/python-dependency/1.0.0",
            "files": [
                {"assetType": "Microsoft.VisualStudio.Code.Manifest",
                    "source": f"{gallery_url}/assets/manifest/python-dependency/1.0.0"},
                {"assetType": "Microsoft.VisualStudio.Services.VSIXPackage",
                    "source": f"{gallery_url}/assets/vsix/python-dependency/1.0.0"},
            ],
            "properties": [{"key": "Microsoft.VisualStudio.Code.Engine", "value": "*"}],
        }
        extension = {
            "extensionId": "dependency-uuid", "extensionName": "python-dependency",
            "displayName": "Python Dependency", "shortDescription": "Dependency fixture",
            "publisher": {"displayName": "BoxTeam", "publisherId": "publisher-id",
                "publisherName": "boxteam-probe"},
            "versions": [version], "statistics": [], "tags": [], "categories": [], "flags": "",
            "releaseDate": "2026-01-01T00:00:00Z", "publishedDate": "2026-01-01T00:00:00Z",
            "lastUpdated": "2026-01-01T00:00:00Z",
        }
        return {"results": [{"extensions": [extension], "resultMetadata": [{"metadataType": "ResultCount",
            "metadataItems": [{"name": "TotalCount", "count": 1}]}]}]}

    @gallery.get("/assets/{source}/python-dependency/1.0.0/{asset_type}")
    async def dependency_asset(source: str, asset_type: str, request: Request):
        if asset_type == "Microsoft.VisualStudio.Code.Manifest":
            gallery_requests.append((f"/{source}/manifest", 200, None))
            return {"name": "python-dependency", "publisher": "boxteam-probe", "version": "1.0.0",
                "engines": {"vscode": "^1.0.0"}, "extensionDependencies": []}
        if asset_type != "Microsoft.VisualStudio.Services.VSIXPackage":
            raise HTTPException(404, "unknown gallery asset")
        redirect = request.query_params.get("redirect") == "true"
        status = 503 if primary_fails and redirect else 200
        encoding = "gzip" if status == 200 else None
        gallery_requests.append((f"/{source}/vsix{'?redirect=true' if redirect else ''}", status, encoding))
        if status != 200:
            return Response("primary CDN unavailable", status_code=status)
        if body_disconnect:
            async def broken_gzip():
                yield compressed_dependency[:24]
                raise OSError("fixture compressed response body disconnected")
            return StreamingResponse(broken_gzip(), media_type="application/octet-stream",
                headers={"Content-Encoding": "gzip"})
        return Response(compressed_dependency, media_type="application/octet-stream",
            headers={"Content-Encoding": "gzip"})

    gallery_url = await serve_extension_application(gallery)
    product = json.loads((context.project_root / "src/clients/workbench/upstream/product.json").read_text())
    metadata = json.loads((context.run_root / "runtime/version.json").read_text())
    product.update({"version": metadata["version"], "commit": metadata["commit"], "quality": "stable",
        "extensionsGallery": {"serviceUrl": f"{gallery_url}/gallery"}})
    preload = context.artifacts_dir / "gateway-dependency-gallery.mjs"
    preload.write_text("globalThis._VSCODE_PRODUCT_JSON = " + json.dumps(product) + ";\n"
        + "globalThis._VSCODE_PACKAGE_JSON = " + json.dumps({"version": metadata["version"]}) + ";\n")
    monkeypatch.setenv("NODE_OPTIONS", f"{os.getenv('NODE_OPTIONS', '')} --import={preload.as_uri()}".strip())
    synthetic_asset_settings = case.domains[1].owner.directory / "data/User/settings.json"
    synthetic_asset_settings.parent.mkdir(parents=True, exist_ok=True)
    synthetic_asset_settings.write_text(json.dumps({"extensions.verifySignature": signature_enabled}))
    for domain_index in (0, 1):
        await _restart_gateway_management(case, domain=domain_index)

    backends: list[WorkspaceContainerRegistry] = []
    targets: list[WorkspaceTarget] = []
    client = await network_scope.enter_async_context(httpx.AsyncClient(timeout=30, trust_env=False))
    for index, domain in enumerate(case.domains):
        backend = _backend(case.workspace / f"download-{index}", case, domain=index)
        backends.append(backend)
        backend_app = FastAPI()
        backend_app.add_middleware(TraceMiddleware)
        backend_app.exception_handler(HTTPException)(workspace_http_exception_handler)
        backend_app.include_router(backend_host_router)
        backend_app.state.workspace_container_registry = backend
        backend_url = await serve_extension_application(backend_app)
        mount = backend.mounted_workspaces.resolve(backend.mounted_workspaces.workspace_ids[0])
        target = WorkspaceTarget(gateway_id=domain.gateway_id, workspace_id=mount.workspace_id,
            name=f"download-{index}", root_path=str(mount.workspace_root), backend_url=backend_url,
            connection_kind="local")
        domain.registry.upsert(target, activate=False)
        targets.append(target)

    a_domain, b_domain = case.domains
    a_backend, b_backend = backends
    b_target = targets[1]
    assert b_domain.owner.instance is not None and b_domain.owner.process is not None
    b_instance = b_domain.owner.instance
    b_process = b_domain.owner.process
    # A 预先有自己的安装状态；安装 B 的依赖时，A 的库存与原件必须完全不变。
    await a_backend.extension_hosts.management.install(vsix_factory(case.workspace, "1.0.0", "existing-a"))
    a_before = await a_backend.extension_hosts.management.inventory()
    a_installation = a_domain.owner.directory / "extensions"
    a_entries = json.loads((a_installation / "extensions.json").read_text())
    [a_package] = a_entries
    a_package_path = a_installation / a_package["relativeLocation"]
    a_package_before = installation_snapshot(a_package_path)

    observed: list[tuple[int, str, str]] = []
    observed_call_sites: list[str] = []
    import app.gateway.server.extension_management as management_routes

    original_download = management_routes.extension_download

    async def capture_download(payload: ExtensionDownloadRequest) -> Response:
        observed_call_sites.append(payload.call_site)
        return await original_download(payload)

    monkeypatch.setattr(management_routes, "extension_download", capture_download)
    for index, domain in enumerate(case.domains):
        original_authorize = domain.owner.authorize_download

        def observe(capability: str, request_id: str, *, owner_index: int = index,
                    authorize: Callable[[str, str], None] = original_authorize) -> None:
            observed.append((owner_index, capability, request_id))
            authorize(capability, request_id)

        monkeypatch.setattr(domain.owner, "authorize_download", observe)
    discovered = await client.post(f"{b_domain.url}/api/gateway/extension-targets",
        headers={**b_domain.headers, "X-Request-ID": "download-target-discovery"},
        json={"protocol_version": 1, "gateway_id": b_domain.gateway_id})
    assert discovered.status_code == 200, discovered.text
    resolved = await client.post(f"{b_domain.url}/api/gateway/extension-targets/resolve",
        headers={**b_domain.headers, "X-Request-ID": "download-backend-discovery"},
        json={"protocol_version": 1, "gateway_id": b_domain.gateway_id,
            "workspace_ids": [b_target.workspace_id], "backend_id": b_backend.extension_hosts.backend_id})
    assert resolved.status_code == 200, resolved.text
    assert b_target.backend_id == b_backend.extension_hosts.backend_id
    assert b_target.root_path is not None
    b_before = await b_backend.extension_hosts.management.inventory()
    source = vsix_factory(Path(b_target.root_path), "1.1.0", "root-b",
        manifest_overrides={"extensionDependencies": [dependency_id]})
    request_id = "gateway-download-b"
    response = await client.post(f"{b_domain.url}/api/gateway/extension-installations/install",
        headers={**b_domain.headers, "X-Request-ID": request_id}, json={
            "protocol_version": 1, "gateway_id": b_domain.gateway_id,
            "backend_id": b_backend.extension_hosts.backend_id, "workspace_ids": [b_target.workspace_id],
            "source_uri": resource_display_uri(scope="workspace", scope_id=b_target.workspace_id,
                kind="files", tail_segments=("root", source.name), gateway_authority=b_domain.gateway_id),
        })
    if signature_enabled or body_disconnect:
        assert response.status_code == 400, response.text
        if signature_enabled:
            assert "SignatureVerificationFailed" in response.text or "NotSigned" in response.text
        else:
            assert "Unable to write file" in response.text and "Error: aborted" in response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        if body_disconnect:
            assert await b_backend.extension_hosts.management.inventory() == b_before
    else:
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
    assert b_domain.owner.instance is b_instance and b_domain.owner.process is b_process and b_process.is_alive()
    assert b_domain.owner.instance.process_id == b_instance.process_id
    b_inventory = await b_backend.extension_hosts.management.inventory()

    def inventory_ids() -> set[str]:
        identifiers: set[str] = set()
        for item in b_inventory.extensions:
            identifier = item.description["identifier"]
            assert isinstance(identifier, dict)
            value = identifier["value"]
            assert isinstance(value, str)
            identifiers.add(value)
        return identifiers

    installed_ids = inventory_ids()
    if signature_enabled or body_disconnect:
        if body_disconnect:
            assert b_inventory == b_before
        else:
            assert dependency_id not in installed_ids
        recovery_source = vsix_factory(Path(b_target.root_path), "2.0.0", "after-signature-rejection",
            manifest_overrides={"publisher": "boxteam-probe", "name": "recovery-probe"})
        recovered = await client.post(f"{b_domain.url}/api/gateway/extension-installations/install",
            headers={**b_domain.headers, "X-Request-ID": "gateway-recovery-b"}, json={
                "protocol_version": 1, "gateway_id": b_domain.gateway_id,
                "backend_id": b_backend.extension_hosts.backend_id, "workspace_ids": [b_target.workspace_id],
                "source_uri": resource_display_uri(scope="workspace", scope_id=b_target.workspace_id,
                    kind="files", tail_segments=("root", recovery_source.name),
                    gateway_authority=b_domain.gateway_id),
            })
        assert recovered.status_code == 200, recovered.text
        assert recovered.headers["X-Request-ID"] == recovered.json()["request_id"] == "gateway-recovery-b"
        b_inventory = await b_backend.extension_hosts.management.inventory()
        installed_ids = inventory_ids()
        assert "boxteam-probe.recovery-probe" in installed_ids
    else:
        assert {"boxteam-probe.target-inventory", dependency_id} <= installed_ids
        published_dependencies = []
        for entry in json.loads((b_domain.owner.directory / "extensions/extensions.json").read_text()):
            package = b_domain.owner.directory / "extensions" / entry["relativeLocation"]
            manifest = json.loads((package / "package.json").read_text())
            if f"{manifest['publisher']}.{manifest['name']}" == dependency_id:
                published_dependencies.append(package / "extension.js")
        assert len(published_dependencies) == 1
        with ZipFile(dependency_vsix) as archive:
            assert published_dependencies[0].read_bytes() == archive.read("extension/extension.js")

    assert observed and all(index == 1 for index, _, _ in observed)
    assert {received_id for _, _, received_id in observed} == {request_id}
    expected_call_sites = ["extensionGalleryService.download"]
    if primary_fails:
        expected_call_sites.append("extensionGalleryService.download.fallback")
    if body_disconnect:
        expected_call_sites *= 3
    assert observed_call_sites == expected_call_sites
    assert await a_backend.extension_hosts.management.inventory() == a_before
    assert installation_snapshot(a_package_path) == a_package_before
    expected_downloads = [
        ("/fallback/vsix?redirect=true", 503 if primary_fails else 200,
            None if primary_fails else "gzip"),
        *([("/fallback/vsix", 200, "gzip")] if primary_fails else []),
    ]
    if body_disconnect:
        expected_downloads *= 3
    assert [item for item in gallery_requests if "/vsix" in item[0]] == expected_downloads
    if body_disconnect:
        assert b_domain.owner.process is b_process and b_process.is_alive()
    assert any(item[0] == "/primary/manifest" and item[1] == 200 for item in gallery_requests)

    # 安装回执结清后，旧 capability 不得再次下载或被误认为当前安装。
    _, old_capability, old_request_id = observed[0]
    before = [item for item in gallery_requests if "/vsix" in item[0]]
    stale = await client.post(
        f"{b_domain.url}/api/gateway/internal/extension-management/"
        f"{b_domain.owner.backend_id}/downloads/{old_capability}",
        headers={"X-Local-Token": case.token, "X-Request-ID": old_request_id},
        json={"call_site": "extensionGalleryService.download", "url": f"{gallery_url}/assets/primary/python-dependency/1.0.0/Microsoft.VisualStudio.Services.VSIXPackage",
            "headers": {}, "timeout_ms": None, "follow_redirects": 0, "proxy_url": None, "strict_ssl": True})
    assert stale.status_code == 403, stale.text
    assert [item for item in gallery_requests if "/vsix" in item[0]] == before
@pytest.mark.parametrize(("retained", "damage"), [
    (retained, damage) for retained in (False, True)
    for damage in ("invalid-manifest", "invalid-json", "missing-manifest", "not-directory")
] + [pytest.param(False, "missing-directory", id="current-missing-directory")])
async def test_native_vsix_keep_existing_rejects_damaged_original_without_writes(
    management_backends, vsix_factory, installation_snapshot, retained: bool, damage: str,
) -> None:
    case = management_backends
    context, workspace = case.context, case.workspace
    registry = _backend(workspace / "target", case)
    management = registry.extension_hosts.management
    published = await management.install(vsix_factory(workspace, "1.0.0", "original"))
    installation = case.domains[0].owner.directory / "extensions"
    profile = installation / "extensions.json"
    original = installation / json.loads(profile.read_text())[0]["relativeLocation"]
    expected_inventory = published.inventory
    if retained:
        expected_inventory = (await management.install(vsix_factory(workspace, "2.0.0", "current-v2"))).inventory
        assert json.loads((installation / ".obsolete").read_text())[original.name] is True
    manifest = original / "package.json"
    if damage == "invalid-manifest":
        content = json.loads(manifest.read_text())
        content["main"] = 42
        manifest.write_text(json.dumps(content))
    elif damage == "invalid-json":
        manifest.write_text("{")
    elif damage == "missing-manifest":
        manifest.unlink()
    else:
        # 故障注入在原安装完成后进行；保留移出的原目录，不用候选重造安装事实。
        original.rename(workspace / "preserved-original")
        if damage == "not-directory":
            original.write_text("existing-path-is-a-file")
    before = installation_snapshot(installation)
    profile_bytes = profile.read_bytes()
    candidate = vsix_factory(workspace, "1.0.0", "candidate-must-not-replace-damaged-original")
    with pytest.raises(ExtensionManagementError) as rejected:
        await management.install(candidate)
    after = installation_snapshot(installation)
    assert after == before
    assert profile.read_bytes() == profile_bytes
    case.domains[0].owner.assert_healthy()
    if retained and damage != "invalid-manifest":
        assert rejected.value.code == "ScanningExtension"
        assert "Cannot read the extension from" in str(rejected.value)
    else:
        assert rejected.value.code == "Invalid"
        diagnostic = "property `main`" if damage == "invalid-manifest" else "JSON" if damage == "invalid-json" else "package.json"
        assert diagnostic in str(rejected.value)
    if retained:
        assert await management.inventory() == expected_inventory
    (context.artifacts_dir / f"vsix-reuse-rejected-{'retained' if retained else 'current'}-{damage}.json").write_text(json.dumps({
        "failure": str(rejected.value), "code": rejected.value.code,
        "snapshot_before": before, "snapshot_after": after,
    }, ensure_ascii=False, indent=2))


@pytest.mark.parametrize("existing", [False, True], ids=["initial", "upgrade"])
@pytest.mark.parametrize(("manifest_case", "overrides", "diagnostic"), [
    ("missing-engines", {"engines": None}, "property `engines` is mandatory"),
    ("invalid-version", {"version": "not-semver"}, "Extension version is not semver compatible"),
    ("invalid-main", {"main": 42}, "property `main` can be omitted or must be of type `string`"),
    ("invalid-browser", {"browser": 42}, "property `browser` can be omitted or must be of type `string`"),
    ("invalid-dependencies", {"extensionDependencies": [42]}, "property `extensionDependencies` can be omitted or must be of type `string[]`"),
    ("invalid-activation", {"activationEvents": [42]}, "property `activationEvents` can be omitted or must be of type `string[]`"),
    ("wildcard-engine", {"engines": {"vscode": "*"}}, "Version specified in `engines.vscode`"),
])
async def test_native_vsix_manifest_preflight_preserves_installation(
    management_backends, vsix_factory, installation_snapshot, existing: bool,
    manifest_case: str, overrides: dict[str, object], diagnostic: str,
) -> None:
    case = management_backends
    context, workspace = case.context, case.workspace
    registry = _backend(workspace / "target", case)
    management = registry.extension_hosts.management
    if existing:
        await management.install(vsix_factory(workspace, "1.0.0", "original"))
    before = await management.inventory()
    installation = case.domains[0].owner.directory / "extensions"
    snapshot = installation_snapshot(installation)
    # 首次安装也核 profile 的存在状态与原字节；拒绝不能先创建一个空安装记录。
    profile = installation / "extensions.json"
    profile_bytes = profile.read_bytes() if profile.exists() else None
    failure = None
    try:
        await management.install(vsix_factory(workspace, "2.0.0", "must-not-publish", manifest_overrides=overrides))
    except ExtensionManagementError as error:
        failure = str(error)
    after_snapshot = installation_snapshot(installation)
    inventory_error = None
    after = None
    try:
        after = await management.inventory()
    except ExtensionManagementError as error:
        inventory_error = str(error)
    (context.artifacts_dir / f"manifest-preflight-{manifest_case}-{'upgrade' if existing else 'initial'}.json").write_text(json.dumps({
        "failure": failure, "inventory_error": inventory_error,
        "before": before.model_dump(), "after": after.model_dump() if after is not None else None,
        "snapshot_before": snapshot, "snapshot_after": after_snapshot,
    }, ensure_ascii=False, indent=2))
    assert after_snapshot == snapshot
    assert (profile.read_bytes() if profile.exists() else None) == profile_bytes
    assert inventory_error is None and after == before
    assert failure is not None and diagnostic in failure
    assert registry.extension_hosts.active == ()


async def test_native_vsix_declarative_engine_rejection_keeps_original_diagnostic(
    management_backends, vsix_factory, installation_snapshot,
) -> None:
    case = management_backends
    workspace = case.workspace
    registry = _backend(workspace / "target", case)
    management = registry.extension_hosts.management
    before = await management.inventory()
    installation = case.domains[0].owner.directory / "extensions"
    snapshot = installation_snapshot(installation)
    # 完整 validator 对纯声明式扩展跳过 engine 版本；原 VSIX install 的拒绝仍必须保留。
    with pytest.raises(ExtensionManagementError, match="not compatible"):
        await management.install(vsix_factory(workspace, "1.0.0", "declarative", engine="^999.0.0",
            omit=("main", "activationEvents", "extensionKind")))
    assert installation_snapshot(installation) == snapshot
    assert await management.inventory() == before


async def test_native_vsix_manifest_warning_and_valid_upgrade_remain_supported(management_backends, vsix_factory) -> None:
    case = management_backends
    workspace = case.workspace
    registry = _backend(workspace / "target", case)
    management = registry.extension_hosts.management
    # 原 validator 将入口越出包根诊断为非便携 Warning；本组只验安装，不激活该入口。
    warning = await management.install(vsix_factory(workspace, "1.0.0", "warning", manifest_overrides={"main": "../extension.js"}))
    extension = warning.inventory.extensions[0]
    assert warning.installed.version == "1.0.0" and extension.is_valid
    assert any(validation.severity == 2 and "Expected `main`" in validation.message for validation in extension.validations)
    previous = case.domains[0].owner.directory / "extensions" / json.loads((case.domains[0].owner.directory / "extensions/extensions.json").read_text())[0]["relativeLocation"]
    previous_bytes = (previous / "package.json").read_bytes()
    upgraded = await management.install(vsix_factory(workspace, "2.0.0", "valid"))
    assert upgraded.installed.version == "2.0.0" and len(upgraded.inventory.extensions) == 1
    assert upgraded.inventory.extensions[0].is_valid and upgraded.inventory.extensions[0].description["version"] == "2.0.0"
    assert (previous / "package.json").read_bytes() == previous_bytes
    assert await management.inventory() == upgraded.inventory
    assert registry.extension_hosts.active == ()


async def test_native_profile_isolated_between_gateways_and_shared_across_backend_restart(
    management_backends, vsix_factory,
) -> None:
    case = management_backends
    context, workspace = case.context, case.workspace
    a = _backend(workspace / "a", case)
    b = _backend(workspace / "b", case, domain=1)
    first, second = a.extension_hosts.management, b.extension_hosts.management
    gateway = case.domains[0].owner
    original_process = gateway.instance
    assert original_process is not None
    assert (await first.inventory()).extensions == []
    a_environment, b_environment = await first.environment(), await second.environment()
    assert a_environment.app_root_uri == (context.run_root / "runtime").as_uri()
    assert a_environment.global_storage_home_uri == (gateway.directory / "data/User/globalStorage").as_uri()
    assert a_environment.workspace_storage_home_uri == (gateway.directory / "data/User/workspaceStorage").as_uri()
    assert b_environment.global_storage_home_uri != a_environment.global_storage_home_uri
    one = (await first.install(vsix_factory(workspace, "1.0.0", "A"))).installed
    two = (await second.install(vsix_factory(workspace, "2.0.0", "B"))).installed
    assert one.id == two.id == "boxteam-probe.target-inventory"
    assert (one.version, two.version) == ("1.0.0", "2.0.0")
    a_inventory, b_inventory = await first.inventory(), await second.inventory()
    assert len(a_inventory.extensions) == len(b_inventory.extensions) == 1
    for inventory, version in ((a_inventory, "1.0.0"), (b_inventory, "2.0.0")):
        extension = inventory.extensions[0]
        assert extension.source == "profile" and extension.is_valid
        identifier = extension.description["identifier"]
        assert isinstance(identifier, dict) and identifier["value"] == one.id
        assert extension.description["version"] == version
        assert extension.description["main"] == "./extension.js"
        assert extension.description["isUnderDevelopment"] is False
    assert a_inventory.extensions[0].description["extensionLocation"] != b_inventory.extensions[0].description["extensionLocation"]
    assert a.extension_hosts.active == b.extension_hosts.active == ()
    duplicate = _backend(workspace / "a-peer", case)
    assert duplicate.extension_hosts.backend_id == a.extension_hosts.backend_id
    assert duplicate.extension_hosts.management is not first
    assert await duplicate.extension_hosts.management.inventory() == a_inventory
    # 两个 Backend 同时写同一原 profile；每份回执的库存包含自己的真实版本。
    publications = await asyncio.gather(
        first.install(vsix_factory(workspace, "3.0.0", "A3")),
        duplicate.extension_hosts.management.install(vsix_factory(workspace, "4.0.0", "A4")))
    for publication, version in zip(publications, ("3.0.0", "4.0.0"), strict=True):
        assert publication.installed.version == publication.inventory.extensions[0].description["version"] == version
    shared_inventory = await first.inventory()
    assert shared_inventory == await duplicate.extension_hosts.management.inventory()
    assert gateway.instance is original_process
    assert gateway.process is not None and gateway.process.is_alive()
    await a.close_extension_hosts()
    a.close()
    assert first.drained and not gateway.drained
    assert gateway.instance is original_process and gateway.process is not None and gateway.process.is_alive()
    recovered = _backend(workspace / "a", case)
    assert await recovered.extension_hosts.management.inventory() == shared_inventory
    assert await recovered.extension_hosts.management.environment() == a_environment
    assert await second.inventory() == b_inventory
    with pytest.raises(ExtensionManagementError, match="not compatible"):
        await recovered.extension_hosts.management.install(vsix_factory(workspace, "5.0.0", "invalid", engine="^999.0.0"))
    assert await recovered.extension_hosts.management.inventory() == shared_inventory
    gateway.assert_healthy()
    assert gateway.instance is original_process
    (context.artifacts_dir / "inventories.json").write_text(json.dumps({"backend_a": recovered.extension_hosts.backend_id,
        "backend_b": b.extension_hosts.backend_id, "management_pid": original_process.pid,
        "a": shared_inventory.model_dump(), "b": b_inventory.model_dump()}, ensure_ascii=False, indent=2))


@pytest.mark.parametrize("root_count", [1, 2], ids=["single-root", "multi-root"])
async def test_configuration_uses_original_session_view_with_persistent_management(
    management_backends, root_count: int,
) -> None:
    case = management_backends
    context, workspace = case.context, case.workspace
    roots = tuple(workspace / f"root-{index}" for index in range(root_count))
    for root in roots:
        root.mkdir()
    registry = _backend(workspace, case, roots=roots)
    management_process = case.domains[0].owner.process
    assert management_process is not None
    owner = registry.extension_hosts
    async with owner.connect(gateway_id="gateway_" + uuid4().hex, expected_backend_id=owner.backend_id,
            client_id="configuration-test", view_id="configuration", workspace_ids=registry.mounted_workspaces.workspace_ids) as connection:
        identity = connection.identity
        files = await owner.configuration_files(ExtensionConfigurationViewRequest(protocol_version=1,
            gateway_id=identity.gateway_id, backend_id=identity.backend_id, workspace_ids=list(identity.workspace_ids),
            view_id=identity.view_id, generation=identity.generation), client_id=identity.client_id)
        initialization = connection.initialization
        resources = [initialization.environment.remote_settings]
        if initialization.workspace.configuration is not None:
            resources.append(initialization.workspace.configuration)
        receipts = []
        for resource in resources:
            native = Path(url2pathname(urlsplit(resource.native_uri).path))
            content = {"editor.fontSize": 17} if resource is resources[0] else {
                "folders": [{"uri": root.as_uri(), "name": root.name} for root in roots], "settings": {"editor.fontSize": 18}}
            encoded = b64encode((json.dumps(content) + "\n").encode()).decode()
            options = ExtensionConfigurationWriteOptions(create=True, overwrite=True, unlock=False, atomic=False, append=False)
            await files.write(resource.resource_uri, encoded, options)
            assert (await files.read(resource.resource_uri)).content_base64 == encoded
            assert case.domains[0].owner.process is management_process
            assert management_process.is_alive()
            # 固定 .75 ms 保证能区分原 Node Date MathRound 与错误的毫秒截断。
            os.utime(native, ns=(1_700_000_000_123_750_000, 1_700_000_000_123_750_000))
            observed = await files.stat(resource.resource_uri)
            command = [str(case.runtime.executable), "--input-type=module", "-e", """
import {statSync} from 'node:fs';
const stat = statSync(process.argv.at(-1));
const permissions = ((stat.mode & 0o200) ? 0 : 2) | ((stat.mode & 0o111) ? 4 : 0);
console.log(JSON.stringify({type: 1, ctime: stat.birthtime.getTime(), mtime: stat.mtime.getTime(),
  size: stat.size, permissions: permissions || null}));
""", str(native)]
            compared = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=10, check=False)
            assert compared.returncode == 0, compared.stderr
            node_stat = json.loads(compared.stdout)
            assert observed.model_dump(exclude={"resource_uri"}) == node_stat
            if resource is resources[0]:
                sibling = _backend(workspace / "shared-profile-backend", case, roots=roots[:1]).extension_hosts
                assert sibling is not owner and sibling.backend_id == owner.backend_id
                async with sibling.connect(gateway_id=identity.gateway_id, expected_backend_id=sibling.backend_id,
                        client_id="shared-configuration", view_id="shared", workspace_ids=identity.workspace_ids[:1]) as other:
                    shared = other.initialization.environment.remote_settings
                    assert shared == resource
                    other_identity = other.identity
                    other_files = await sibling.configuration_files(ExtensionConfigurationViewRequest(protocol_version=1,
                        gateway_id=other_identity.gateway_id, backend_id=other_identity.backend_id,
                        workspace_ids=list(other_identity.workspace_ids), view_id=other_identity.view_id,
                        generation=other_identity.generation), client_id=other_identity.client_id)
                    assert (await other_files.read(shared.resource_uri)).content_base64 == encoded
            if resource is not resources[0]:
                invalid = b64encode(json.dumps({"folders": [{"uri": root.as_uri()} for root in roots[::-1]]}).encode()).decode()
                before = native.read_bytes()
                with pytest.raises(ExtensionManagementError, match="有序工作区根"):
                    await files.write(resource.resource_uri, invalid, options)
                assert native.read_bytes() == before
            native.unlink()
            for operation in (files.read, files.stat):
                with pytest.raises(ExtensionManagementError) as missing:
                    await operation(resource.resource_uri)
                assert missing.value.code == "EntryNotFound"
                assert not native.exists()
            receipts.append({"resource_uri": resource.resource_uri, "deleted_remains_missing": not native.exists(),
                "node_stat_command": command, "node_stat_exit_code": compared.returncode, "node_stat_stdout": compared.stdout,
                "node_stat_stderr": compared.stderr, "python_stat": observed.model_dump()})
        case.domains[0].owner.assert_healthy()
        assert case.domains[0].owner.process is management_process
        (context.artifacts_dir / f"configuration-{root_count}.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2))


@pytest.mark.parametrize("damage", ["duplicate", "missing_manifest", "invalid_json"])
async def test_inventory_reports_damaged_install_facts_without_empty_success(management_backends, vsix_factory, damage: str) -> None:
    case = management_backends
    workspace = case.workspace
    registry = _backend(workspace / "target", case)
    management = registry.extension_hosts.management
    await management.install(vsix_factory(workspace, "1.0.0", "original"))
    inventory = await management.inventory()
    assert len(inventory.extensions) == 1
    profile = case.domains[0].owner.directory / "extensions/extensions.json"
    members = json.loads(profile.read_text())
    assert len(members) == 1
    original = profile.read_bytes()
    if damage == "duplicate":
        profile.write_text(json.dumps(members + members))
        expected = "重复扩展 ID"
    elif damage == "invalid_json":
        profile.write_text("{")
        expected = "JSON"
    else:
        (case.domains[0].owner.directory / "extensions" / members[0]["relativeLocation"] / "package.json").unlink()
        # 原扫描器保留 profile 中损坏安装的身份，以 invalid 和原验证原因报告，不能过滤成空成功。
        damaged = await management.inventory()
        assert len(damaged.extensions) == 1
        extension = damaged.extensions[0]
        assert not extension.is_valid and extension.description["id"] == "boxteam-probe.target-inventory"
        assert "main" not in extension.description
        assert any("package.json" in validation.message for validation in extension.validations)
        assert profile.read_bytes() == original
        return
    original = profile.read_bytes()
    with pytest.raises(ExtensionManagementError, match=expected):
        await management.inventory()
    assert profile.read_bytes() == original
    if damage == "invalid_json":
        gateway = case.domains[0].owner
        gateway.assert_healthy()
        assert gateway.instance is not None and gateway.process is not None and gateway.process.is_alive()
        assert profile.read_bytes() == original
        (case.context.artifacts_dir / "profile-damage-shutdown.json").write_text(json.dumps({
            "outcome": "单次原生库存错误不影响共享管理服务", "process_id": gateway.instance.process_id,
            "runtime_instance_id": gateway.instance.runtime_instance_id,
        }, ensure_ascii=False, indent=2))


async def test_cancelled_backend_request_keeps_shared_process_and_settles_original_reply(
    management_backends, vsix_factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = management_backends
    registry = _backend(case.workspace / "target", case)
    peer = _backend(case.workspace / "peer", case)
    management = registry.extension_hosts.management
    gateway = case.domains[0].owner
    original_instance = gateway.instance
    assert original_instance is not None and gateway._client is not None
    received, deliver = asyncio.Event(), asyncio.Event()
    original_post = gateway._client.post
    replies: list[dict[str, JsonValue]] = []

    async def hold_delivery(*args, **kwargs):
        response = await original_post(*args, **kwargs)
        if kwargs.get("json", {}).get("method") == "install" and not received.is_set():
            replies.append(response.json())
            received.set()
            await deliver.wait()
        return response

    monkeypatch.setattr(gateway._client, "post", hold_delivery)
    operation = asyncio.create_task(management.install(vsix_factory(case.workspace, "1.0.0", "published")))
    closing = None
    try:
        async with asyncio.timeout(10):
            await received.wait()
            operation.cancel()
            closing = asyncio.create_task(gateway.close(preserve=True))
            await asyncio.sleep(0)
            assert not operation.done() and not closing.done()
            assert gateway.instance is original_instance and gateway.process is not None and gateway.process.is_alive()
            deliver.set()
            with pytest.raises(asyncio.CancelledError):
                await operation
            await closing
        assert gateway.drained and not gateway.process
        assert len(replies) == 1
        await _restart_gateway_management(case)
        inventory = await peer.extension_hosts.management.inventory()
        assert [entry.description["version"] for entry in inventory.extensions] == ["1.0.0"]
        assert case.domains[0].owner.instance is not None
        assert case.domains[0].owner.instance.process_id == original_instance.process_id
    finally:
        deliver.set()
        if not operation.done():
            with pytest.raises(asyncio.CancelledError):
                await operation
        if closing is not None and not closing.done():
            await closing


@pytest.mark.parametrize(
    ("failure", "expected_detail"),
    [
        ("malformed", "扩展管理控制回执不完整"),
        ("missing-code", "扩展管理控制回执不完整"),
        ("error-object", "扩展管理错误回执字段无效"),
        ("error-null", "扩展管理错误回执字段无效"),
    ],
)
async def test_unknown_control_reply_fails_gateway_without_replay_or_backend_takeover(
    management_backends, vsix_factory, monkeypatch: pytest.MonkeyPatch,
    failure: str, expected_detail: str,
) -> None:
    case = management_backends
    registry = _backend(case.workspace / "target", case)
    management = registry.extension_hosts.management
    gateway = case.domains[0].owner
    assert (await management.inventory()).extensions == []
    original_instance = gateway.instance
    assert original_instance is not None and gateway._client is not None
    original_post = gateway._client.post
    replies: list[dict[str, JsonValue]] = []

    async def damage_delivery(*args, **kwargs):
        response = await original_post(*args, **kwargs)
        if kwargs.get("json", {}).get("method") != "install":
            return response
        value = response.json()
        replies.append(value)
        assert "result" in value
        if failure == "malformed":
            payload = {"result": value["result"], "unexpected": True}
        elif failure == "missing-code":
            payload = {"error": "缺少错误码"}
        else:
            payload = {"error": {} if failure == "error-object" else None, "code": "Unknown"}
        return httpx.Response(response.status_code, headers=response.headers, json=payload, request=response.request)

    monkeypatch.setattr(gateway._client, "post", damage_delivery)
    with pytest.raises(httpx.HTTPStatusError) as rejected:
        await management.install(vsix_factory(case.workspace, "1.0.0", "published"))
    assert rejected.value.response.status_code == 409
    assert rejected.value.response.json()["detail"] == expected_detail
    assert len(replies) == 1
    with pytest.raises(RuntimeError, match="不自动重启或重放") as unhealthy:
        gateway.assert_healthy()
    original_failure = unhealthy.value.__cause__
    assert isinstance(original_failure, ValueError) and str(original_failure) == expected_detail
    assert gateway.instance is original_instance and gateway.process is not None and gateway.process.is_alive()
    # 关闭、重建 Backend client 不能接管 Gateway owner，也不能把未知写入再发一次。
    await registry.close_extension_hosts()
    registry.close()
    recovered = _backend(case.workspace / "target", case)
    with pytest.raises(httpx.HTTPStatusError) as still_failed:
        await recovered.extension_hosts.management.install(vsix_factory(case.workspace, "2.0.0", "must-not-install"))
    assert still_failed.value.response.status_code == 500
    assert len(replies) == 1 and gateway.instance is original_instance
    old_process = gateway.process
    await gateway.close()
    assert gateway.drained and old_process is not None and not old_process.is_alive()
    # 显式重启真实 Gateway 管理 owner 才恢复读取，旧安装只执行一次且没有发布 V2。
    await _restart_gateway_management(case)
    inventory = await recovered.extension_hosts.management.inventory()
    assert [entry.description["version"] for entry in inventory.extensions] == ["1.0.0"]
    assert case.domains[0].owner.instance is not None
    assert case.domains[0].owner.instance.runtime_instance_id != original_instance.runtime_instance_id
    assert len(replies) == 1


async def test_gateway_handoff_detaches_and_adopts_same_management_service(management_backends, vsix_factory) -> None:
    case = management_backends
    registry = _backend(case.workspace / "target", case)
    gateway = case.domains[0].owner
    original = gateway.instance
    assert original is not None and gateway.process is not None and gateway.process.is_alive()
    management = registry.extension_hosts.management
    await management.install(vsix_factory(case.workspace, "1.0.0", "persisted-through-handoff"))
    await gateway.close(preserve=True)
    assert gateway.drained
    assert gateway.process is None
    await _restart_gateway_management(case)
    adopted = case.domains[0].owner
    assert adopted.instance is not None and adopted.process is not None and adopted.process.is_alive()
    assert adopted.instance.runtime_instance_id == original.runtime_instance_id
    assert adopted.instance.process_id == original.process_id
    inventory = await management.inventory()
    assert [item.description["version"] for item in inventory.extensions] == ["1.0.0"]


async def test_gateway_inventory_reads_installed_target_profile_and_rejects_reused_address(
    management_backends, vsix_factory, installation_snapshot, network_scope: AsyncExitStack,
    serve_extension_application: Callable[[FastAPI], Awaitable[str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = management_backends
    context, workspace = case.context, case.workspace
    first = _backend(workspace / "a", case)
    second = _backend(workspace / "b", case, domain=1)
    token = case.token
    primary = case.domains[0]
    gateway_id, url, headers = primary.gateway_id, primary.url, primary.headers
    client = await network_scope.enter_async_context(httpx.AsyncClient(timeout=10, trust_env=False))
    apps: list[FastAPI] = []
    targets: list[WorkspaceTarget] = []
    for backend, domain in zip((first, second), case.domains, strict=True):
        application = FastAPI()
        application.add_middleware(TraceMiddleware)
        application.exception_handler(HTTPException)(workspace_http_exception_handler)
        application.include_router(backend_host_router)
        application.state.workspace_container_registry = backend
        backend_url = await serve_extension_application(application)
        apps.append(application)
        mount = backend.mounted_workspaces.resolve(backend.mounted_workspaces.workspace_ids[0])
        target = WorkspaceTarget(gateway_id=domain.gateway_id, workspace_id=mount.workspace_id, name="same-name",
            root_path=str(mount.workspace_root), backend_url=backend_url, connection_kind="local")
        domain.registry.upsert(target, activate=False)
        targets.append(target)
    backend_ids = tuple(backend.extension_hosts.backend_id for backend in (first, second))
    processes = tuple(domain.owner.process for domain in case.domains)
    receipts: list[dict[str, object]] = []

    # 两个真实 Gateway 各返回自己的安装域；Backend 来源通过原认证 resolve 填原路由身份。
    for index, (domain, target) in enumerate(zip(case.domains, targets, strict=True)):
        request_id = f"domain-discovery-{index}"
        discovered = await client.post(f"{domain.url}/api/gateway/extension-targets",
            headers={**domain.headers, "X-Request-ID": request_id},
            json={"protocol_version": 1, "gateway_id": domain.gateway_id})
        assert discovered.status_code == 200, discovered.text
        assert discovered.headers["X-Request-ID"] == discovered.json()["request_id"] == request_id
        [installation_domain] = discovered.json()["data"]["targets"]
        assert installation_domain["backend_id"] == backend_ids[index]
        assert "workspace_ids" not in installation_domain
        resolved = await client.post(f"{domain.url}/api/gateway/extension-targets/resolve",
            headers={**domain.headers, "X-Request-ID": f"backend-discovery-{index}"},
            json={"protocol_version": 1, "gateway_id": domain.gateway_id, "workspace_ids": [target.workspace_id],
                "backend_id": backend_ids[index]})
        assert resolved.status_code == 200, resolved.text
        assert target.backend_id == backend_ids[index]
        receipts.append({"request_id": request_id, "status": discovered.status_code, "body": discovered.json()})
    assert first.extension_hosts.active == second.extension_hosts.active == ()
    assert processes[0].pid != processes[1].pid

    # 真实 B 来源不能替浏览器更换已选 A 安装域，拒绝前后两个原 profile 均为空。
    assert targets[1].root_path is not None
    wrong_source = vsix_factory(Path(targets[1].root_path), "9.0.0", "must-not-install")
    cross_domain = await client.post(f"{url}/api/gateway/extension-installations/install",
        headers={**headers, "X-Request-ID": "install-source-cannot-change-domain"}, json={
            "protocol_version": 1, "gateway_id": gateway_id, "backend_id": backend_ids[0],
            "workspace_ids": [targets[1].workspace_id],
            "source_uri": resource_display_uri(scope="workspace", scope_id=targets[1].workspace_id, kind="files",
                tail_segments=("root", wrong_source.name), gateway_authority=gateway_id)})
    assert cross_domain.status_code == 409, cross_domain.text
    assert cross_domain.headers["X-Request-ID"] == cross_domain.json()["request_id"] == "install-source-cannot-change-domain"
    assert (await first.extension_hosts.management.inventory()).extensions == []
    assert (await second.extension_hosts.management.inventory()).extensions == []
    assert all(domain.owner.process is process for domain, process in zip(case.domains, processes, strict=True))
    receipts.append({"request_id": "install-source-cannot-change-domain", "status": cross_domain.status_code, "body": cross_domain.json()})

    async def inventory(index: int, backend_id: str, request_id: str) -> httpx.Response:
        domain = case.domains[index]
        response = await client.post(f"{domain.url}/api/gateway/extension-installations/inventory",
            headers={**domain.headers, "X-Request-ID": request_id}, json={"protocol_version": 1,
                "gateway_id": domain.gateway_id, "backend_id": backend_id})
        body = response.json()
        assert response.headers["X-Request-ID"] == body["request_id"] == request_id
        receipts.append({"request_id": request_id, "status": response.status_code, "body": body})
        return response

    async def install(index: int, version: str, *, engine: str = "^1.0.0",
                      manifest_overrides: dict[str, object] | None = None) -> httpx.Response:
        target, domain = targets[index], case.domains[index]
        assert target.root_path is not None
        source = vsix_factory(Path(target.root_path), version, version, engine=engine, manifest_overrides=manifest_overrides)
        request_id = f"install-{index}-{version}"
        response = await client.post(f"{domain.url}/api/gateway/extension-installations/install",
            headers={**domain.headers, "X-Request-ID": request_id}, json={"protocol_version": 1,
                "gateway_id": domain.gateway_id, "workspace_ids": [target.workspace_id],
                "backend_id": backend_ids[index],
                "source_uri": resource_display_uri(scope="workspace", scope_id=target.workspace_id, kind="files",
                    tail_segments=("root", source.name), gateway_authority=domain.gateway_id)})
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        receipts.append({"request_id": request_id, "status": response.status_code, "body": response.json()})
        return response

    for index, version in ((0, "1.0.0"), (1, "2.0.0")):
        empty = await inventory(index, backend_ids[index], f"empty-{index}")
        assert empty.json()["data"]["inventory"]["extensions"] == []
        installed = await install(index, version)
        assert installed.status_code == 200, installed.text
        result = installed.json()["data"]
        assert result["installed"] == {"id": "boxteam-probe.target-inventory", "version": version,
            "profile_id": result["inventory"]["profile_id"], "reused": False}
        assert result["inventory"]["extensions"][0]["description"]["version"] == version

    # 库存直接读取所属 Gateway 的原 profile，安装仍经真实 Backend 来源验证与私有 HTTP。
    a, b = await asyncio.gather(inventory(0, backend_ids[0], "inventory-a"), inventory(1, backend_ids[1], "inventory-b"))
    for index, (response, backend, version) in enumerate(((a, first, "1.0.0"), (b, second, "2.0.0"))):
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert result["backend_id"] == backend_ids[index]
        assert result["gateway_id"] == case.domains[index].gateway_id and "workspace_ids" not in result
        assert result["profile_scope"] == "default"
        assert result["inventory"] == (await backend.extension_hosts.management.inventory()).model_dump()
        assert len(result["inventory"]["extensions"]) == 1
        extension = result["inventory"]["extensions"][0]
        assert extension["description"]["version"] == version and extension["is_valid"]
    previous = b.json()["data"]
    # Backend B 离线不阻塞任一 Gateway 已启动的管理服务。
    case.domains[1].registry.upsert(replace(targets[1], connection_error="B 已离线"), activate=False)
    isolated = await inventory(0, backend_ids[0], "inventory-a-with-b-offline")
    assert isolated.status_code == 200 and isolated.json()["data"] == a.json()["data"]
    assert (await inventory(1, backend_ids[1], "inventory-b-with-own-backend-offline")).json()["data"] == previous
    case.domains[1].registry.upsert(targets[1], activate=False)
    failed = await install(0, "3.0.0", engine="^999.0.0")
    assert failed.status_code == 400 and "not compatible" in failed.text
    assert (await inventory(0, first.extension_hosts.backend_id, "failed-install-kept")).json()["data"] == a.json()["data"]
    directory = case.domains[0].owner.directory / "extensions"
    snapshot = installation_snapshot(directory)
    profile_bytes = (directory / "extensions.json").read_bytes()
    malformed = await install(0, "3.1.0", manifest_overrides={"browser": 42})
    assert malformed.status_code == 400 and "property `browser` can be omitted or must be of type `string`" in malformed.text
    assert installation_snapshot(directory) == snapshot
    assert (directory / "extensions.json").read_bytes() == profile_bytes
    assert (await inventory(0, first.extension_hosts.backend_id, "invalid-manifest-kept")).json()["data"] == a.json()["data"]
    assert (await inventory(1, second.extension_hosts.backend_id, "peer-after-invalid-manifest")).json()["data"] == previous
    [member] = json.loads((directory / "extensions.json").read_text())
    old_script = directory / member["relativeLocation"] / "extension.js"
    original_bytes = old_script.read_bytes()
    updated = await install(0, "4.0.0")
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["installed"]["version"] == "4.0.0"
    assert updated.json()["data"]["inventory"]["extensions"][0]["description"]["version"] == "4.0.0"
    assert old_script.read_bytes() == original_bytes
    assert (await inventory(1, second.extension_hosts.backend_id, "peer-after-update")).json()["data"] == previous
    native_install = first.extension_hosts.management.install
    published, deliver = asyncio.Event(), asyncio.Event()

    async def hold_first_publication(path: Path) -> InstalledProfile:
        publication = await native_install(path)
        if publication.installed.version == "5.0.0":
            # 原Node安装及库存已经生成，只暂停第一份HTTP回执，允许下一次真实安装先完成。
            published.set()
            await deliver.wait()
        return publication

    monkeypatch.setattr(first.extension_hosts.management, "install", hold_first_publication)
    delayed = asyncio.create_task(install(0, "5.0.0"))
    try:
        async with asyncio.timeout(10):
            await published.wait()
            newer = await install(0, "6.0.0")
    finally:
        deliver.set()
        earlier = await delayed
    for response, version in ((earlier, "5.0.0"), (newer, "6.0.0")):
        assert response.status_code == 200, response.text
        publication = response.json()["data"]
        assert publication["installed"]["version"] == publication["inventory"]["extensions"][0]["description"]["version"] == version
    original_owner = second.extension_hosts
    backend_id = original_owner.backend_id
    await second.close_extension_hosts()
    second.close()
    recovered = _backend(workspace / "b", case, domain=1)
    apps[1].state.workspace_container_registry = recovered
    assert (await inventory(1, backend_id, "inventory-recovered")).json()["data"] == previous
    assert (await inventory(1, str(uuid4()), "inventory-wrong-backend")).status_code == 409
    assert case.domains[1].owner.process is processes[1] and processes[1].is_alive()
    # 来源路由误指另一真实 Backend 时，原 Backend 核实稳定身份后拒绝，不能把安装写到 A。
    case.domains[1].registry.upsert(replace(targets[1], backend_url=targets[0].backend_url), activate=False)
    rejected = await install(1, "9.1.0")
    assert rejected.status_code == 409, rejected.text
    assert "Backend" in rejected.json()["detail"]
    assert (await first.extension_hosts.management.inventory()).extensions[0].description["version"] == "6.0.0"
    assert (await recovered.extension_hosts.management.inventory()).extensions[0].description["version"] == "2.0.0"
    assert first.extension_hosts.active == recovered.extension_hosts.active == original_owner.active == ()
    case.domains[1].registry.upsert(targets[1], activate=False)

    selection = {"protocol_version": 1, "gateway_id": gateway_id, "backend_id": first.extension_hosts.backend_id,
        "profile_id": earlier.json()["data"]["inventory"]["profile_id"],
        "extension_id": "boxteam-probe.target-inventory", "version": "6.0.0"}
    rejected = await client.post(f"{url}/api/gateway/extension-installations/uninstall",
        headers={**headers, "X-Request-ID": "uninstall-stale-version"}, json={**selection, "version": "5.0.0"})
    assert rejected.status_code == 409 and rejected.headers["X-Request-ID"] == rejected.json()["request_id"] == "uninstall-stale-version"
    assert (await first.extension_hosts.management.inventory()).extensions[0].description["version"] == "6.0.0"
    retained = {path: installation_snapshot(path) for path in directory.iterdir() if path.is_dir()}
    native_request = primary.owner.request
    published.clear()
    deliver.clear()

    async def hold_uninstall_publication(command: dict[str, JsonValue], *, download_base_url: str | None = None) -> dict[str, JsonValue]:
        publication = await native_request(command, download_base_url=download_base_url)
        if command["method"] == "uninstall":
            published.set()
            await deliver.wait()
        return publication

    monkeypatch.setattr(primary.owner, "request", hold_uninstall_publication)
    delayed_uninstall = asyncio.create_task(client.post(f"{url}/api/gateway/extension-installations/uninstall",
        headers={**headers, "X-Request-ID": "uninstall-original-publication"}, json=selection))
    try:
        async with asyncio.timeout(10):
            await published.wait()
            assert (await install(0, "7.0.0")).status_code == 200
    finally:
        deliver.set()
        removed = await delayed_uninstall
    assert removed.status_code == 200, removed.text
    assert removed.headers["X-Request-ID"] == removed.json()["request_id"] == "uninstall-original-publication"
    removal = removed.json()["data"]
    assert "workspace_ids" not in removal
    assert removal["uninstalled"] == {"id": selection["extension_id"], "version": "6.0.0", "profile_id": selection["profile_id"]}
    assert removal["inventory"]["extensions"] == []
    assert (await first.extension_hosts.management.inventory()).extensions[0].description["version"] == "7.0.0"
    assert (await recovered.extension_hosts.management.inventory()).model_dump() == previous["inventory"]
    assert all(installation_snapshot(path) == snapshot for path, snapshot in retained.items())
    assert first.extension_hosts.active == recovered.extension_hosts.active == ()
    receipts.append({"request_id": "uninstall-original-publication", "status": removed.status_code, "body": removed.json()})

    current_inventory = await first.extension_hosts.management.inventory()
    [current_extension] = current_inventory.extensions
    profile_id = current_inventory.profile_id
    extension_key = current_extension.extension_key
    extension_id = current_extension.description["id"]
    assert isinstance(extension_id, str)
    extension_resource = extension_package_uri(gateway_id=gateway_id, backend_id=first.extension_hosts.backend_id,
        profile_id=profile_id, extension_id=extension_id, relative_segments=("extension.js",))
    async def package_read(request_id: str, *, generation: str | None = None, view_id: str | None = None,
                          selected_key: str = extension_key) -> httpx.Response:
        payload: dict[str, object] = {"protocol_version": 1, "gateway_id": gateway_id,
            "backend_id": first.extension_hosts.backend_id, "resource_uri": extension_resource,
            "extension_key": selected_key}
        if generation is not None:
            payload["generation"] = generation
            payload["view_id"] = view_id
        response = await client.post(f"{url}/api/gateway/extension-packages/read",
            headers={**headers, "X-Request-ID": request_id}, json=payload)
        assert response.headers.get("X-Request-ID") == response.json().get("request_id") == request_id
        return response

    async def native_package_read(request_id: str, relative_segments: list[str], *, binding: dict[str, object]) -> httpx.Response:
        generation, view_id, client_id = (binding[key] for key in ("generation", "view_id", "client_id"))
        assert isinstance(generation, str) and isinstance(view_id, str) and isinstance(client_id, str)
        return await client.post(f"{targets[0].backend_url}/api/internal/extension-files/read",
            headers={"X-Local-Token": token, "X-BoxTeam-Gateway-Id": gateway_id, "X-Request-ID": request_id,
                "X-BoxTeam-Extension-Client-Id": client_id},
            json={"protocol_version": 1, "gateway_id": gateway_id,
                  "backend_id": first.extension_hosts.backend_id, "profile_id": profile_id,
                  "extension_id": extension_id, "extension_key": extension_key,
                  "relative_segments": relative_segments, "current_profile_member": False,
                  "generation": generation, "view_id": view_id})

    ws_url = url.replace("http://", "ws://") + "/api/gateway/extension-web-hosts/channel"

    async def open_web_view(view_id: str, request_id: str):
        channel = await connect(ws_url, additional_headers={**headers, "X-Request-ID": request_id}, compression=None)
        await channel.send(json.dumps({"protocol_version": 1, "gateway_id": gateway_id,
            "backend_id": first.extension_hosts.backend_id, "view_id": view_id, "workspace_ids": [],
            "profile_id": profile_id, "extension_keys": [extension_key]}))
        binding = json.loads(await channel.recv())
        assert binding["request_id"] == request_id and binding["view_id"] == view_id
        assert binding["workspace_ids"] == [] and binding["extension_keys"] == [extension_key]
        assert [entry["extension_key"] for entry in binding["inventory"]["extensions"]] == [extension_key]
        return channel, binding

    channel_a, binding_a = await open_web_view("package-window-a", "package-web-open-a")
    try:
        channel_b, binding_b = await open_web_view("package-window-b", "package-web-open-b")
        try:
            assert binding_a["client_id"] == binding_b["client_id"]
            assert binding_a["generation"] != binding_b["generation"]
            package_root = case.domains[0].owner.directory / "extensions" / extension_key
            invalid_id = "invalid-extension-path-" + uuid4().hex
            rejected_path = await native_package_read(invalid_id, ["..", "outside.txt"], binding=binding_a)
            assert rejected_path.status_code == 403, rejected_path.text
            assert rejected_path.headers["X-Request-ID"] == rejected_path.json()["request_id"] == invalid_id
            assert rejected_path.json()["detail"]["code"] == "NoPermissions"

            large_package_file = package_root / "large.bin"
            large_package_file.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
            try:
                oversized_id = "oversized-extension-file-" + uuid4().hex
                oversized_package = await native_package_read(oversized_id, ["large.bin"], binding=binding_a)
                assert oversized_package.status_code == 413, oversized_package.text
                assert oversized_package.headers["X-Request-ID"] == oversized_package.json()["request_id"] == oversized_id
                assert oversized_package.json()["detail"]["code"] == "EntryTooLarge"
            finally:
                large_package_file.unlink()

            original_package = await package_read("package-current-a", generation=binding_a["generation"],
                view_id=binding_a["view_id"])
            assert original_package.status_code == 200, original_package.text
            original_bytes = b64decode(original_package.json()["data"]["content_base64"])
            assert original_bytes == b'exports.activate = () => "7.0.0";\n'
            second_package = await package_read("package-current-b", generation=binding_b["generation"],
                view_id=binding_b["view_id"])
            assert second_package.status_code == 200
            cross_view = await package_read("package-cross-view", generation=binding_a["generation"],
                view_id=binding_b["view_id"])
            assert cross_view.status_code == 409

            upgraded = await install(0, "8.0.0")
            assert upgraded.status_code == 200
            stale_current = await package_read("package-old-current")
            assert stale_current.status_code == 409
            frozen_after_upgrade = await package_read("package-frozen-after-upgrade-a",
                generation=binding_a["generation"], view_id=binding_a["view_id"])
            assert frozen_after_upgrade.status_code == 200
            assert b64decode(frozen_after_upgrade.json()["data"]["content_base64"]) == original_bytes
            removed_package = await client.post(f"{url}/api/gateway/extension-installations/uninstall",
                headers={**headers, "X-Request-ID": "package-uninstall-current"}, json={
                    "protocol_version": 1, "gateway_id": gateway_id, "backend_id": first.extension_hosts.backend_id,
                    "profile_id": profile_id, "extension_id": extension_id, "version": "8.0.0"})
            assert removed_package.status_code == 200, removed_package.text
            assert removed_package.headers["X-Request-ID"] == removed_package.json()["request_id"] == "package-uninstall-current"
            for binding in (binding_a, binding_b):
                retained_read = await package_read(f"package-retained-{binding['view_id']}",
                    generation=binding["generation"], view_id=binding["view_id"])
                assert retained_read.status_code == 200
                assert b64decode(retained_read.json()["data"]["content_base64"]) == original_bytes

            await channel_a.close()
            async with asyncio.timeout(10):
                while binding_a["generation"] in first.extension_hosts._sessions:
                    await asyncio.sleep(0.01)
            closed_a = await package_read("package-closed-a", generation=binding_a["generation"],
                view_id=binding_a["view_id"])
            assert closed_a.status_code == 409
            assert (await package_read("package-open-b-after-a-close", generation=binding_b["generation"],
                view_id=binding_b["view_id"])).status_code == 200
        finally:
            await channel_b.close()
        async with asyncio.timeout(10):
            while binding_b["generation"] in first.extension_hosts._sessions:
                await asyncio.sleep(0.01)
        for binding in (binding_a, binding_b):
            closed = await package_read(f"package-closed-{binding['view_id']}",
                generation=binding["generation"], view_id=binding["view_id"])
            assert closed.status_code == 409
        assert first.extension_hosts.active == ()
        receipts.append({"request_id": "package-web-binding-a", "body": binding_a,
            "request_id_b": "package-web-binding-b", "body_b": binding_b,
            "retained_after_upgrade_uninstall": True, "closed_views_rejected": True})
    finally:
        await channel_a.close()

    (context.artifacts_dir / "gateway-inventory.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2) + "\n")


async def test_native_profile_uninstall_keeps_all_versions_and_rejects_stale_selection(
    management_backends, vsix_factory, installation_snapshot,
) -> None:
    case = management_backends
    workspace = case.workspace
    first = _backend(workspace / "a", case).extension_hosts.management
    second = _backend(workspace / "b", case, domain=1).extension_hosts.management
    original = vsix_factory(workspace, "1.0.0", "v1-remains-readable")
    await first.install(original)
    current = await first.install(vsix_factory(workspace, "2.0.0", "v2-remains-readable"))
    await first.install(vsix_factory(workspace, "1.0.0", "unrelated", manifest_overrides={"name": "unrelated"}))
    peer = await second.install(vsix_factory(workspace, "9.0.0", "other-target"))
    profile_id, extension_id = current.installed.profile_id, current.installed.id
    directory = case.domains[0].owner.directory / "extensions"
    profile = directory / "extensions.json"
    profile_bytes = profile.read_bytes()
    retained = {path: installation_snapshot(path) for path in directory.iterdir() if path.is_dir()}
    assert len(retained) == 3
    for selection in (
        {"profile_id": "stale-profile", "extension_id": extension_id, "version": "2.0.0"},
        {"profile_id": profile_id, "extension_id": "boxteam-probe.missing", "version": "2.0.0"},
        {"profile_id": profile_id, "extension_id": extension_id, "version": "1.0.0"},
    ):
        with pytest.raises(ExtensionManagementError) as caught:
            await first.uninstall(**selection)
        assert caught.value.code == ("Invalid" if selection["profile_id"] != profile_id else "InstalledExtensionNotFound")
        assert profile.read_bytes() == profile_bytes
        assert all(installation_snapshot(path) == snapshot for path, snapshot in retained.items())
        assert await second.inventory() == peer.inventory

    removed = await first.uninstall(profile_id=profile_id, extension_id=extension_id, version="2.0.0")
    assert removed.uninstalled.model_dump() == {"id": extension_id, "version": "2.0.0", "profile_id": profile_id}
    remaining = []
    for entry in removed.inventory.extensions:
        identifier = entry.description["identifier"]
        assert isinstance(identifier, dict)
        remaining.append((identifier["value"], entry.description["version"]))
    assert remaining == [("boxteam-probe.unrelated", "1.0.0")]
    assert await first.inventory() == removed.inventory
    assert all(installation_snapshot(path) == snapshot for path, snapshot in retained.items())
    with pytest.raises(ExtensionManagementError) as caught:
        await first.uninstall(profile_id=profile_id, extension_id=extension_id, version="2.0.0")
    assert caught.value.code == "InstalledExtensionNotFound" and await first.inventory() == removed.inventory
    reused = await first.install(original)
    assert reused.installed.reused and reused.installed.version == "1.0.0"
    assert all(installation_snapshot(path) == snapshot for path, snapshot in retained.items())
    assert await second.inventory() == peer.inventory


async def test_native_profile_uninstall_retains_original_dependency_failure_and_other_target(
    management_backends, vsix_factory, installation_snapshot,
) -> None:
    case = management_backends
    workspace = case.workspace
    first = _backend(workspace / "a", case).extension_hosts.management
    second = _backend(workspace / "b", case, domain=1).extension_hosts.management
    base = await first.install(vsix_factory(workspace, "1.0.0", "dependency"))
    dependent = await first.install(vsix_factory(workspace, "1.0.0", "dependent", manifest_overrides={
        "name": "dependent", "extensionDependencies": [base.installed.id]}))
    peer = await second.install(vsix_factory(workspace, "2.0.0", "other-target"))
    directory = case.domains[0].owner.directory / "extensions"
    before = installation_snapshot(directory)
    with pytest.raises(ExtensionManagementError, match="depends on this"):
        await first.uninstall(profile_id=base.installed.profile_id, extension_id=base.installed.id, version="1.0.0")
    assert installation_snapshot(directory) == before
    assert await first.inventory() == dependent.inventory
    assert await second.inventory() == peer.inventory
    gateway = case.domains[0].owner
    process = gateway.instance
    gateway.assert_healthy()
    removed_dependent = await first.uninstall(profile_id=dependent.installed.profile_id,
        extension_id=dependent.installed.id, version=dependent.installed.version)
    assert len(removed_dependent.inventory.extensions) == 1
    removed_base = await first.uninstall(profile_id=base.installed.profile_id,
        extension_id=base.installed.id, version=base.installed.version)
    assert removed_base.inventory.extensions == []
    gateway.assert_healthy()
    assert gateway.instance is process
    assert await second.inventory() == peer.inventory
