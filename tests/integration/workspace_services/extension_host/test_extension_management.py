"""真实原生安装／profile 扫描及 Backend 进程回收；窗口管理 UI 尚不在本切片。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4
from zipfile import ZipFile

import httpx
import portalocker
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.api.extension_host import router as backend_host_router
from app.container import WorkspaceContainerRegistry
from app.core.mounted_workspaces import MountedWorkspace, MountedWorkspaceRegistry
from app.core.resource_uri import resource_display_uri
from app.core.trace_middleware import TraceMiddleware
from app.core.workspace_identity import load_or_create_workspace_id
from app.gateway.auth import get_gateway_local_token
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.user_access import USER_ACCESS_COOKIE_NAME, UserAccessService
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.main import gateway_http_exception_handler
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.server.extension_host import router as gateway_host_router
from app.main import workspace_http_exception_handler
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
    InstalledProfile,
)
from app.services.infrastructure.extension_host.runtime import NodeHostRuntime
from tests.harness.python.run_context import TestRunContext


@pytest.fixture(scope="module")
def management_runtime() -> tuple[TestRunContext, Path, NodeHostRuntime]:
    executable, modules = os.getenv("BOXTEAM_TEST_NODE_HOST_EXECUTABLE"), os.getenv("BOXTEAM_TEST_NODE_HOST_MODULES")
    if not executable or not modules:
        pytest.skip("UNMET_PREREQUISITE: 需显式提供固定 Node 及原扩展管理依赖")
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
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


@pytest.fixture
async def management_backends(
    management_runtime: tuple[TestRunContext, Path, NodeHostRuntime], monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[TestRunContext, Path, list[WorkspaceContainerRegistry]]]:
    context, component, runtime = management_runtime
    context.artifacts_dir.mkdir(parents=True, exist_ok=True)
    home = context.boxteam_home_for_node(request.node.nodeid)
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BOXTEAM_HOME", str(home))
    manifest = home / "runtime-manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "extension_host": {
        "node_executable": str(runtime.executable), "component_root": str(component)}}))
    monkeypatch.setenv("BOXTEAM_RUNTIME_MANIFEST", str(manifest))
    workspace = context.workspace_root / context.run_root.name / uuid4().hex
    workspace.mkdir(parents=True)
    backends: list[WorkspaceContainerRegistry] = []
    try:
        yield context, workspace, backends
    finally:
        for registry in backends:
            # 原 owner 负责同时停止宿主／管理进程；记录实际原句柄的退出收据。
            owner = registry._extension_hosts
            assert owner is not None
            await registry.close_extension_hosts()
            if owner._management is not None:
                context.artifacts_dir.mkdir(parents=True, exist_ok=True)
                (context.artifacts_dir / f"process-{owner.backend_id}-{uuid4()}.json").write_text(json.dumps(asdict(owner._management.child.state)))
                assert owner._management.child.state.exit_verified
            registry.close()


def _backend(workspace: Path, backends: list[WorkspaceContainerRegistry], *, backend_id: str | None = None) -> WorkspaceContainerRegistry:
    workspace.mkdir(exist_ok=True)
    registry = WorkspaceContainerRegistry(backend_id=backend_id or str(uuid4()), mounted_workspaces=MountedWorkspaceRegistry([
        MountedWorkspace(load_or_create_workspace_id(workspace), workspace)]))
    backends.append(registry)
    return registry


@pytest.fixture
def vsix_factory():
    def create(directory: Path, version: str, content: str, *, engine: str = "^1.0.0") -> Path:
        vsix = directory / f"probe-{version}-{uuid4()}.vsix"
        manifest = {"name": "target-inventory", "publisher": "boxteam-probe", "version": version,
            "engines": {"vscode": engine}, "main": "./extension.js", "extensionKind": ["workspace"],
            "activationEvents": ["onCommand:boxteam.inventory"],
            "contributes": {"commands": [{"command": "boxteam.inventory", "title": "库存测试"}]}}
        with ZipFile(vsix, "w") as archive:
            archive.writestr("extension/package.json", json.dumps(manifest))
            archive.writestr("extension/extension.js", f"exports.activate = () => {json.dumps(content)};\n")
        return vsix
    return create


async def test_native_vsix_profile_inventory_isolated_and_recovered(management_backends, vsix_factory) -> None:
    context, workspace, backends = management_backends
    a = _backend(workspace / "a", backends)
    b = _backend(workspace / "b", backends)
    first, second = a.extension_hosts.management, b.extension_hosts.management
    assert (await first.inventory()).extensions == []
    a_environment, b_environment = await first.environment(), await second.environment()
    assert a_environment.app_root_uri == (context.run_root / "runtime").as_uri()
    assert a_environment.global_storage_home_uri == (first._directory / "data/User/globalStorage").as_uri()
    assert a_environment.workspace_storage_home_uri == (first._directory / "data/User/workspaceStorage").as_uri()
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
    assert not a.extension_hosts.drained
    duplicate = _backend(workspace / "a", backends, backend_id=a.extension_hosts.backend_id)
    with pytest.raises(portalocker.exceptions.LockException):
        await duplicate.extension_hosts.management.inventory()
    assert duplicate.extension_hosts.management.child.state.phase == "not_started"
    assert await first.inventory() == a_inventory
    with pytest.raises(RuntimeError, match="尚未回收"):
        a.close()
    await a.close_extension_hosts()
    assert first.child.state.exit_verified and first.child.state.returncode == 0
    a.close()
    assert a._extension_hosts is not None
    recovered = _backend(workspace / "a", backends, backend_id=a._extension_hosts.backend_id)
    assert await recovered.extension_hosts.management.inventory() == a_inventory
    assert await recovered.extension_hosts.management.environment() == a_environment
    assert await second.inventory() == b_inventory
    with pytest.raises(ExtensionManagementError, match="not compatible"):
        await recovered.extension_hosts.management.install(vsix_factory(workspace, "3.0.0", "invalid", engine="^999.0.0"))
    assert await recovered.extension_hosts.management.inventory() == a_inventory
    (context.artifacts_dir / "inventories.json").write_text(json.dumps({"backend_a": recovered.extension_hosts.backend_id,
        "backend_b": b.extension_hosts.backend_id, "a": a_inventory.model_dump(), "b": b_inventory.model_dump()}, ensure_ascii=False, indent=2))


@pytest.mark.parametrize("damage", ["duplicate", "missing_manifest", "invalid_json"])
async def test_inventory_reports_damaged_install_facts_without_empty_success(management_backends, vsix_factory, damage: str) -> None:
    _, workspace, backends = management_backends
    registry = _backend(workspace / "target", backends)
    management = registry.extension_hosts.management
    await management.install(vsix_factory(workspace, "1.0.0", "original"))
    inventory = await management.inventory()
    assert len(inventory.extensions) == 1
    profile = management._directory / "extensions/extensions.json"
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
        (management._directory / "extensions" / members[0]["relativeLocation"] / "package.json").unlink()
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


@pytest.mark.parametrize("failure", ["cancelled", "malformed", "error-object", "error-null"])
async def test_unknown_control_reply_reaps_original_process_and_never_replays_install(
    management_backends, vsix_factory, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    _, workspace, backends = management_backends
    registry = _backend(workspace / "target", backends)
    backend_id = registry.extension_hosts.backend_id
    management = registry.extension_hosts.management
    assert (await management.inventory()).extensions == []
    received = asyncio.Event()
    original_receive = management._receive

    async def hold_delivery():
        # 真实原安装已完成、回执到达后才暂停或损坏交付；不由测试手工写profile。
        value = await original_receive()
        assert "result" in value
        received.set()
        if failure == "malformed":
            return {"result": {}}
        if failure in {"error-object", "error-null"}:
            return {"error": {} if failure == "error-object" else None}
        await asyncio.Event().wait()
        return value

    monkeypatch.setattr(management, "_receive", hold_delivery)
    operation = asyncio.create_task(management.install(vsix_factory(workspace, "1.0.0", "published")))
    async with asyncio.timeout(10):
        await received.wait()
        if failure == "cancelled":
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        elif failure == "malformed":
            with pytest.raises(ValidationError, match="inventory"):
                await operation
        else:
            with pytest.raises(TypeError, match="错误回执不是字符串"):
                await operation
    original_pid = management.child.state.pid
    assert management.child.state.exit_verified and original_pid is not None
    with pytest.raises(RuntimeError, match="不能自动重启并重放"):
        await management.install(vsix_factory(workspace, "2.0.0", "must-not-install"))
    assert management.child.state.pid == original_pid
    recovered = _backend(workspace / "target", backends, backend_id=backend_id)
    inventory = await recovered.extension_hosts.management.inventory()
    assert len(inventory.extensions) == 1 and inventory.extensions[0].description["version"] == "1.0.0"


async def test_backend_remains_open_until_management_log_and_lock_are_released(
    management_backends, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, workspace, backends = management_backends
    registry = _backend(workspace / "target", backends)
    owner = registry.extension_hosts
    management = owner.management
    await management.inventory()
    output = management._output
    assert output is not None
    failure = OSError("原日志关闭失败")
    pending = [failure]

    def close() -> None:
        if pending:
            raise pending.pop()
        output.close()

    failing_output = Mock(wraps=output)
    failing_output.close.side_effect = close
    monkeypatch.setattr(management, "_output", failing_output)
    with pytest.raises(BaseExceptionGroup) as caught:
        await registry.close_extension_hosts()
    assert caught.value.exceptions == (failure,)
    assert management.child.state.exit_verified and not output.closed
    assert not owner.drained
    with pytest.raises(portalocker.exceptions.LockException), portalocker.Lock(management._directory / "management.lock", mode="a", timeout=0):
        pytest.fail("原日志关闭失败时不能释放管理锁")
    with pytest.raises(RuntimeError, match="尚未回收"):
        registry.close()
    await registry.close_extension_hosts()
    assert owner.drained and output.closed
    with portalocker.Lock(management._directory / "management.lock", mode="a", timeout=0):
        pass
    registry.close()


async def test_gateway_inventory_reads_installed_target_profile_and_rejects_reused_address(
    management_backends, vsix_factory, network_scope: AsyncExitStack,
    serve_extension_application: Callable[[FastAPI], Awaitable[str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, workspace, backends = management_backends
    first = _backend(workspace / "a", backends)
    second = _backend(workspace / "b", backends)
    token = get_gateway_local_token()
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", token)
    gateway_id = "gateway_" + uuid4().hex
    registry = GatewayWorkspaceRegistry(storage_path=workspace / "workspaces.json", gateway_id=gateway_id)
    network_scope.callback(registry.close)
    client = await network_scope.enter_async_context(httpx.AsyncClient(timeout=10))
    apps: list[FastAPI] = []
    targets: list[WorkspaceTarget] = []
    for backend in (first, second):
        application = FastAPI()
        application.add_middleware(TraceMiddleware)
        application.exception_handler(HTTPException)(workspace_http_exception_handler)
        application.include_router(backend_host_router)
        application.state.workspace_container_registry = backend
        url = await serve_extension_application(application)
        apps.append(application)
        mount = backend.mounted_workspaces.resolve(backend.mounted_workspaces.workspace_ids[0])
        target = WorkspaceTarget(gateway_id=gateway_id, workspace_id=mount.workspace_id, name="same-name",
            root_path=str(mount.workspace_root), backend_url=url, connection_kind="local")
        registry.upsert(target, activate=False)
        targets.append(target)
    state = GatewayStateStore(path=workspace / "gateway.sqlite")
    network_scope.callback(state.close)
    users = UserAccessService(state=state)
    users.create_user(display_name="扩展库存", user_id="inventory")
    access = users.acquire_user(user_id="inventory", client_label="real-network")
    gateway = FastAPI()
    gateway.add_middleware(TraceMiddleware)
    gateway.exception_handler(HTTPException)(gateway_http_exception_handler)
    gateway.include_router(gateway_host_router)
    gateway.state.registry = registry
    gateway.state.user_access_service = users
    gateway.state.federation_policy_store = FederationPolicyStore()
    gateway.state.http_client = client
    url = await serve_extension_application(gateway)
    headers = {"X-Local-Token": token, "Cookie": f"{USER_ACCESS_COOKIE_NAME}={access.access_session_id}"}
    receipts: list[dict[str, object]] = []

    async def inventory(index: int, backend_id: str, request_id: str) -> httpx.Response:
        response = await client.post(f"{url}/api/gateway/extension-installations/inventory",
            headers={**headers, "X-Request-ID": request_id}, json={"protocol_version": 1,
                "gateway_id": gateway_id, "workspace_ids": [targets[index].workspace_id], "backend_id": backend_id})
        body = response.json()
        assert response.headers["X-Request-ID"] == body["request_id"] == request_id
        receipts.append({"request_id": request_id, "status": response.status_code, "body": body})
        return response

    async def install(index: int, version: str, *, engine: str = "^1.0.0") -> httpx.Response:
        target = targets[index]
        backend = (first, second)[index]
        assert target.root_path is not None
        source = vsix_factory(Path(target.root_path), version, version, engine=engine)
        request_id = f"install-{index}-{version}"
        response = await client.post(f"{url}/api/gateway/extension-installations/install",
            headers={**headers, "X-Request-ID": request_id}, json={"protocol_version": 1,
                "gateway_id": gateway_id, "workspace_ids": [target.workspace_id],
                "backend_id": backend.extension_hosts.backend_id,
                "source_uri": resource_display_uri(scope="workspace", scope_id=target.workspace_id, kind="files",
                    tail_segments=("root", source.name), gateway_authority=gateway_id)})
        assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
        receipts.append({"request_id": request_id, "status": response.status_code, "body": response.json()})
        return response

    for index, version in ((0, "1.0.0"), (1, "2.0.0")):
        empty = await inventory(index, (first, second)[index].extension_hosts.backend_id, f"empty-{index}")
        assert empty.json()["data"]["inventory"]["extensions"] == []
        installed = await install(index, version)
        assert installed.status_code == 200, installed.text
        result = installed.json()["data"]
        assert result["installed"] == {"id": "boxteam-probe.target-inventory", "version": version,
            "profile_id": result["inventory"]["profile_id"]}
        assert result["inventory"]["extensions"][0]["description"]["version"] == version

    # 库存正文来自原安装器生成的 profile，经过真实 HTTP／认证／Backend／Node，而非测试回执。
    a, b = await asyncio.gather(inventory(0, first.extension_hosts.backend_id, "inventory-a"),
                                inventory(1, second.extension_hosts.backend_id, "inventory-b"))
    for response, backend, target, version in ((a, first, targets[0], "1.0.0"), (b, second, targets[1], "2.0.0")):
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert result["backend_id"] == backend.extension_hosts.backend_id
        assert result["gateway_id"] == gateway_id and result["workspace_ids"] == [target.workspace_id]
        assert result["profile_scope"] == "default"
        assert result["inventory"] == (await backend.extension_hosts.management.inventory()).model_dump()
        assert len(result["inventory"]["extensions"]) == 1
        extension = result["inventory"]["extensions"][0]
        assert extension["description"]["version"] == version and extension["is_valid"]
    previous = b.json()["data"]
    failed = await install(0, "3.0.0", engine="^999.0.0")
    assert failed.status_code == 400 and "not compatible" in failed.text
    assert (await inventory(0, first.extension_hosts.backend_id, "failed-install-kept")).json()["data"] == a.json()["data"]
    directory = first.extension_hosts.management._directory / "extensions"
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
    recovered = _backend(workspace / "b", backends, backend_id=backend_id)
    apps[1].state.workspace_container_registry = recovered
    assert (await inventory(1, backend_id, "inventory-recovered")).json()["data"] == previous
    assert (await inventory(1, first.extension_hosts.backend_id, "inventory-wrong-backend")).status_code == 409
    # 旧登记指向另一真实 Backend 时不读取对方同 ID 的扩展，也不修改原安装事实。
    registry.upsert(replace(targets[1], backend_url=targets[0].backend_url), activate=False)
    rejected = await inventory(1, backend_id, "inventory-reused-route")
    assert rejected.status_code == 409 and "Backend" in rejected.json()["detail"]
    assert (await first.extension_hosts.management.inventory()).extensions[0].description["version"] == "6.0.0"
    assert (await recovered.extension_hosts.management.inventory()).extensions[0].description["version"] == "2.0.0"
    assert first.extension_hosts.active == recovered.extension_hosts.active == original_owner.active == ()
    (context.artifacts_dir / "gateway-inventory.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2) + "\n")
