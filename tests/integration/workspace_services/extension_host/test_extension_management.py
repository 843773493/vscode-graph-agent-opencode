"""真实原生安装／profile 扫描及 Backend 进程回收；窗口管理 UI 尚不在本切片。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from base64 import b64decode, b64encode
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit
from urllib.request import url2pathname
from uuid import uuid4
from zipfile import ZipFile

import httpx
import portalocker
import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError
from websockets.asyncio.client import connect

from app.api.extension_host import router as backend_host_router
from app.container import WorkspaceContainerRegistry
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
from app.gateway.server.extension_host import router as gateway_host_router
from app.gateway.server.extension_package import router as gateway_package_router
from app.main import workspace_http_exception_handler
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationViewRequest,
    ExtensionConfigurationWriteOptions,
)
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
    InstalledProfile,
    UninstalledProfile,
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
    context, workspace, backends = management_backends
    registry = _backend(workspace / "target", backends)
    management = registry.extension_hosts.management
    published = await management.install(vsix_factory(workspace, "1.0.0", "published-v1",
        manifest_overrides={"displayName": "已发布的 V1"}))
    assert published.installed.reused is False
    installation = management._directory / "extensions"
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
    assert management.drained and registry.extension_hosts.active == ()
    (context.artifacts_dir / f"vsix-reuse-{'retained' if retained else 'current'}.json").write_text(json.dumps({
        "published": published.model_dump(), "result": reused.model_dump(),
        "original_snapshot": original_snapshot, "after_snapshot": installation_snapshot(original),
        "published_script_sha256": sha256(original_script).hexdigest(),
        "candidate_script_sha256": sha256(candidate_script).hexdigest(),
    }, ensure_ascii=False, indent=2))


async def test_native_published_extension_files_are_read_by_exact_retained_key(
    management_backends, vsix_factory, network_scope: AsyncExitStack,
    serve_extension_application: Callable[[FastAPI], Awaitable[str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, workspace, backends = management_backends
    registry = _backend(workspace / "target", backends)
    management = registry.extension_hosts.management
    original = await management.install(vsix_factory(workspace, "1.0.0", "published-v1"))
    profile_id = original.installed.profile_id
    key = "boxteam-probe.target-inventory-1.0.0"
    root = management._directory / "extensions"
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
    token = get_gateway_local_token()
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", token)
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.exception_handler(HTTPException)(workspace_http_exception_handler)
    application.include_router(backend_host_router)
    application.state.workspace_container_registry = registry
    url = await serve_extension_application(application)
    client = await network_scope.enter_async_context(httpx.AsyncClient(timeout=10))
    gateway_id = "gateway_" + uuid4().hex
    request_id = "retained-extension-read-" + uuid4().hex
    backend_id = registry.extension_hosts.backend_id
    response = await client.post(f"{url}/api/internal/extension-files/read",
        headers={"X-Local-Token": token, "X-BoxTeam-Gateway-Id": gateway_id, "X-Request-ID": request_id},
        json={"protocol_version": 1, "gateway_id": gateway_id, "backend_id": backend_id,
              "profile_id": profile_id, "extension_id": "boxteam-probe.target-inventory",
              "extension_key": current_v2_key, "relative_segments": ["extension.js"],
              "current_profile_member": True})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == request_id
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
    assert management.drained and registry.extension_hosts.active == ()
    (context.artifacts_dir / "published-extension-read.json").write_text(json.dumps({
        "profile_id": profile_id, "extension_key": key, "stat": stat.model_dump(),
        "read_size": len(b64decode(read.content_base64)), "old_version_read_after_upgrade": True,
    }, ensure_ascii=False, indent=2))


@pytest.mark.parametrize("retained", [False, True], ids=["current-version", "retained-version"])
async def test_native_vsix_keep_existing_plans_dependencies_from_published_manifest(
    management_backends, vsix_factory, installation_snapshot, serve_extension_application,
    monkeypatch: pytest.MonkeyPatch, retained: bool,
) -> None:
    context, workspace, backends = management_backends
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
    registry = _backend(workspace / "target", backends)
    management = registry.extension_hosts.management
    published_dependency, published_pack = "boxteam-probe.published-dependency", "boxteam-probe.published-pack"
    candidate_dependency, candidate_pack = "boxteam-probe.candidate-dependency", "boxteam-probe.candidate-pack"
    published = await management.install(vsix_factory(workspace, "1.0.0", "published-v1", manifest_overrides={
        "extensionDependencies": [published_dependency], "extensionPack": [published_pack]}))
    assert any(json.dumps(published_dependency) in body and json.dumps(published_pack) in body for body in requests)
    installation = management._directory / "extensions"
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


@pytest.mark.parametrize(("retained", "damage"), [
    (retained, damage) for retained in (False, True)
    for damage in ("invalid-manifest", "invalid-json", "missing-manifest", "not-directory")
] + [pytest.param(False, "missing-directory", id="current-missing-directory")])
async def test_native_vsix_keep_existing_rejects_damaged_original_without_writes(
    management_backends, vsix_factory, installation_snapshot, retained: bool, damage: str,
) -> None:
    context, workspace, backends = management_backends
    registry = _backend(workspace / "target", backends)
    management = registry.extension_hosts.management
    published = await management.install(vsix_factory(workspace, "1.0.0", "original"))
    installation = management._directory / "extensions"
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
    assert management.drained
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
@pytest.mark.parametrize(("case", "overrides", "diagnostic"), [
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
    case: str, overrides: dict[str, object], diagnostic: str,
) -> None:
    context, workspace, backends = management_backends
    registry = _backend(workspace / "target", backends)
    management = registry.extension_hosts.management
    if existing:
        await management.install(vsix_factory(workspace, "1.0.0", "original"))
    before = await management.inventory()
    installation = management._directory / "extensions"
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
    (context.artifacts_dir / f"manifest-preflight-{case}-{'upgrade' if existing else 'initial'}.json").write_text(json.dumps({
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
    _, workspace, backends = management_backends
    registry = _backend(workspace / "target", backends)
    management = registry.extension_hosts.management
    before = await management.inventory()
    installation = management._directory / "extensions"
    snapshot = installation_snapshot(installation)
    # 完整 validator 对纯声明式扩展跳过 engine 版本；原 VSIX install 的拒绝仍必须保留。
    with pytest.raises(ExtensionManagementError, match="not compatible"):
        await management.install(vsix_factory(workspace, "1.0.0", "declarative", engine="^999.0.0",
            omit=("main", "activationEvents", "extensionKind")))
    assert installation_snapshot(installation) == snapshot
    assert await management.inventory() == before


async def test_native_vsix_manifest_warning_and_valid_upgrade_remain_supported(management_backends, vsix_factory) -> None:
    _, workspace, backends = management_backends
    registry = _backend(workspace / "target", backends)
    management = registry.extension_hosts.management
    # 原 validator 将入口越出包根诊断为非便携 Warning；本组只验安装，不激活该入口。
    warning = await management.install(vsix_factory(workspace, "1.0.0", "warning", manifest_overrides={"main": "../extension.js"}))
    extension = warning.inventory.extensions[0]
    assert warning.installed.version == "1.0.0" and extension.is_valid
    assert any(validation.severity == 2 and "Expected `main`" in validation.message for validation in extension.validations)
    previous = management._directory / "extensions" / json.loads((management._directory / "extensions/extensions.json").read_text())[0]["relativeLocation"]
    previous_bytes = (previous / "package.json").read_bytes()
    upgraded = await management.install(vsix_factory(workspace, "2.0.0", "valid"))
    assert upgraded.installed.version == "2.0.0" and len(upgraded.inventory.extensions) == 1
    assert upgraded.inventory.extensions[0].is_valid and upgraded.inventory.extensions[0].description["version"] == "2.0.0"
    assert (previous / "package.json").read_bytes() == previous_bytes
    assert await management.inventory() == upgraded.inventory
    assert registry.extension_hosts.active == ()


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
    assert a.extension_hosts.drained
    duplicate = _backend(workspace / "a", backends, backend_id=a.extension_hosts.backend_id)
    assert await duplicate.extension_hosts.management.inventory() == a_inventory
    assert duplicate.extension_hosts.management.drained
    previous = first.child.state.instance_id
    assert await first.inventory() == a_inventory
    assert first.child.state.instance_id != previous and first.drained
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


@pytest.mark.parametrize("root_count", [1, 2], ids=["single-root", "multi-root"])
async def test_configuration_uses_original_session_view_across_fresh_management_processes(
    management_backends, root_count: int,
) -> None:
    context, workspace, backends = management_backends
    roots = tuple(workspace / f"root-{index}" for index in range(root_count))
    for root in roots:
        root.mkdir()
    registry = WorkspaceContainerRegistry(backend_id=str(uuid4()), mounted_workspaces=MountedWorkspaceRegistry([
        MountedWorkspace(load_or_create_workspace_id(root), root) for root in roots]))
    backends.append(registry)
    owner = registry.extension_hosts
    async with owner.connect(gateway_id="gateway_" + uuid4().hex, expected_backend_id=owner.backend_id,
            client_id="configuration-test", view_id="configuration", workspace_ids=registry.mounted_workspaces.workspace_ids) as connection:
        identity = connection.identity
        files = owner.configuration_files(ExtensionConfigurationViewRequest(protocol_version=1,
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
            previous = owner.management.child.state.instance_id
            assert (await files.read(resource.resource_uri)).content_base64 == encoded
            assert owner.management.child.state.instance_id != previous
            assert (await files.stat(resource.resource_uri)).size == native.stat().st_size
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
            receipts.append({"resource_uri": resource.resource_uri, "deleted_remains_missing": not native.exists()})
        assert owner.management.drained
        (context.artifacts_dir / f"configuration-{root_count}.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2))


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


@pytest.mark.parametrize("failure", ["cancelled", "malformed", "missing-code", "error-object", "error-null"])
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
        if value in ({"ready": True}, {"drained": True}):
            return value
        assert "result" in value
        received.set()
        if failure == "malformed":
            return {"result": {}}
        if failure == "missing-code":
            return {"error": "缺少错误码"}
        if failure in {"error-object", "error-null"}:
            return {"error": {} if failure == "error-object" else None, "code": "Unknown"}
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
        elif failure == "missing-code":
            with pytest.raises(ValueError, match="控制回执不完整"):
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
    failure = OSError("原日志关闭失败")
    blocked = True
    original_receive = management._receive
    output = None

    async def inject_log_failure():
        nonlocal output
        value = await original_receive()
        if value == {"ready": True}:
            output = management._output
            assert output is not None

            def close() -> None:
                if blocked:
                    raise failure
                assert output is not None
                output.close()

            failing_output = Mock(wraps=output)
            failing_output.close.side_effect = close
            monkeypatch.setattr(management, "_output", failing_output)
        return value

    monkeypatch.setattr(management, "_receive", inject_log_failure)
    with pytest.raises(OSError) as request_error:
        await management.inventory()
    assert request_error.value is failure
    with pytest.raises(BaseExceptionGroup) as caught:
        await registry.close_extension_hosts()
    assert caught.value.exceptions == (failure,)
    assert output is not None
    assert management.child.state.exit_verified and not output.closed
    assert not owner.drained
    with pytest.raises(portalocker.exceptions.LockException), portalocker.Lock(management._directory / "management.lock", mode="a", timeout=0):
        pytest.fail("原日志关闭失败时不能释放管理锁")
    with pytest.raises(RuntimeError, match="尚未回收"):
        registry.close()
    blocked = False
    await registry.close_extension_hosts()
    assert owner.drained and output.closed
    with portalocker.Lock(management._directory / "management.lock", mode="a", timeout=0):
        pass
    registry.close()


async def test_gateway_inventory_reads_installed_target_profile_and_rejects_reused_address(
    management_backends, vsix_factory, installation_snapshot, network_scope: AsyncExitStack,
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
    gateway.include_router(gateway_package_router)
    gateway.state.registry = registry
    gateway.state.user_access_service = users
    gateway.state.federation_policy_store = FederationPolicyStore()
    gateway.state.http_client = client
    url = await serve_extension_application(gateway)
    headers = {"X-Local-Token": token, "Cookie": f"{USER_ACCESS_COOKIE_NAME}={access.access_session_id}"}
    receipts: list[dict[str, object]] = []

    # 显式目录从真实 Backend 入口确认安装域；不手工向 registry 写入身份投影。
    discovered = await client.post(f"{url}/api/gateway/extension-targets", headers={**headers, "X-Request-ID": "domain-discovery"},
        json={"protocol_version": 1, "gateway_id": gateway_id})
    assert discovered.status_code == 200, discovered.text
    assert discovered.headers["X-Request-ID"] == discovered.json()["request_id"] == "domain-discovery"
    domains = discovered.json()["data"]["targets"]
    assert {domain["backend_id"] for domain in domains} == {first.extension_hosts.backend_id, second.extension_hosts.backend_id}
    assert all("workspace_ids" not in domain for domain in domains)
    assert [target.backend_id for target in targets] == [first.extension_hosts.backend_id, second.extension_hosts.backend_id]
    assert first.extension_hosts.active == second.extension_hosts.active == ()
    assert first.extension_hosts._management is second.extension_hosts._management is None
    receipts.append({"request_id": "domain-discovery", "status": discovered.status_code, "body": discovered.json()})

    # 真实 B 来源不能替浏览器更换已选 A 安装域，拒绝发生在任何管理 child 启动前。
    assert targets[1].root_path is not None
    wrong_source = vsix_factory(Path(targets[1].root_path), "9.0.0", "must-not-install")
    cross_domain = await client.post(f"{url}/api/gateway/extension-installations/install",
        headers={**headers, "X-Request-ID": "install-source-cannot-change-domain"}, json={
            "protocol_version": 1, "gateway_id": gateway_id, "backend_id": first.extension_hosts.backend_id,
            "workspace_ids": [targets[1].workspace_id],
            "source_uri": resource_display_uri(scope="workspace", scope_id=targets[1].workspace_id, kind="files",
                tail_segments=("root", wrong_source.name), gateway_authority=gateway_id)})
    assert cross_domain.status_code == 409, cross_domain.text
    assert cross_domain.headers["X-Request-ID"] == cross_domain.json()["request_id"] == "install-source-cannot-change-domain"
    assert first.extension_hosts._management is second.extension_hosts._management is None
    receipts.append({"request_id": "install-source-cannot-change-domain", "status": cross_domain.status_code, "body": cross_domain.json()})

    async def inventory(index: int, backend_id: str, request_id: str) -> httpx.Response:
        response = await client.post(f"{url}/api/gateway/extension-installations/inventory",
            headers={**headers, "X-Request-ID": request_id}, json={"protocol_version": 1,
                "gateway_id": gateway_id, "backend_id": backend_id})
        body = response.json()
        assert response.headers["X-Request-ID"] == body["request_id"] == request_id
        receipts.append({"request_id": request_id, "status": response.status_code, "body": body})
        return response

    async def install(index: int, version: str, *, engine: str = "^1.0.0",
                      manifest_overrides: dict[str, object] | None = None) -> httpx.Response:
        target = targets[index]
        backend = (first, second)[index]
        assert target.root_path is not None
        source = vsix_factory(Path(target.root_path), version, version, engine=engine, manifest_overrides=manifest_overrides)
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
            "profile_id": result["inventory"]["profile_id"], "reused": False}
        assert result["inventory"]["extensions"][0]["description"]["version"] == version

    # 库存正文来自原安装器生成的 profile，经过真实 HTTP／认证／Backend／Node，而非测试回执。
    a, b = await asyncio.gather(inventory(0, first.extension_hosts.backend_id, "inventory-a"),
                                inventory(1, second.extension_hosts.backend_id, "inventory-b"))
    for response, backend, target, version in ((a, first, targets[0], "1.0.0"), (b, second, targets[1], "2.0.0")):
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert result["backend_id"] == backend.extension_hosts.backend_id
        assert result["gateway_id"] == gateway_id and "workspace_ids" not in result
        assert result["profile_scope"] == "default"
        assert result["inventory"] == (await backend.extension_hosts.management.inventory()).model_dump()
        assert len(result["inventory"]["extensions"]) == 1
        extension = result["inventory"]["extensions"][0]
        assert extension["description"]["version"] == version and extension["is_valid"]
    previous = b.json()["data"]
    # B 离线时，精确 A 的库存仍经原 Gateway／Backend／Node 读取，不重新发现 B。
    registry.upsert(replace(targets[1], connection_error="B 已离线"), activate=False)
    isolated = await inventory(0, first.extension_hosts.backend_id, "inventory-a-with-b-offline")
    assert isolated.status_code == 200 and isolated.json()["data"] == a.json()["data"]
    registry.upsert(targets[1], activate=False)
    failed = await install(0, "3.0.0", engine="^999.0.0")
    assert failed.status_code == 400 and "not compatible" in failed.text
    assert (await inventory(0, first.extension_hosts.backend_id, "failed-install-kept")).json()["data"] == a.json()["data"]
    directory = first.extension_hosts.management._directory / "extensions"
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
    recovered = _backend(workspace / "b", backends, backend_id=backend_id)
    apps[1].state.workspace_container_registry = recovered
    assert (await inventory(1, backend_id, "inventory-recovered")).json()["data"] == previous
    assert (await inventory(1, str(uuid4()), "inventory-wrong-backend")).status_code == 409
    # 旧登记指向另一真实 Backend 时不读取对方同 ID 的扩展，也不修改原安装事实。
    registry.upsert(replace(targets[1], backend_url=targets[0].backend_url), activate=False)
    rejected = await inventory(1, backend_id, "inventory-reused-route")
    assert rejected.status_code == 409 and "Backend" in rejected.json()["detail"]
    assert (await first.extension_hosts.management.inventory()).extensions[0].description["version"] == "6.0.0"
    assert (await recovered.extension_hosts.management.inventory()).extensions[0].description["version"] == "2.0.0"
    assert first.extension_hosts.active == recovered.extension_hosts.active == original_owner.active == ()
    registry.upsert(targets[1], activate=False)

    selection = {"protocol_version": 1, "gateway_id": gateway_id, "backend_id": first.extension_hosts.backend_id,
        "profile_id": earlier.json()["data"]["inventory"]["profile_id"],
        "extension_id": "boxteam-probe.target-inventory", "version": "6.0.0"}
    rejected = await client.post(f"{url}/api/gateway/extension-installations/uninstall",
        headers={**headers, "X-Request-ID": "uninstall-stale-version"}, json={**selection, "version": "5.0.0"})
    assert rejected.status_code == 409 and rejected.headers["X-Request-ID"] == rejected.json()["request_id"] == "uninstall-stale-version"
    assert (await first.extension_hosts.management.inventory()).extensions[0].description["version"] == "6.0.0"
    retained = {path: installation_snapshot(path) for path in directory.iterdir() if path.is_dir()}
    native_uninstall = first.extension_hosts.management.uninstall
    published.clear()
    deliver.clear()

    async def hold_uninstall_publication(**member: str) -> UninstalledProfile:
        publication = await native_uninstall(**member)
        published.set()
        await deliver.wait()
        return publication

    monkeypatch.setattr(first.extension_hosts.management, "uninstall", hold_uninstall_publication)
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

    async def native_package_read(request_id: str, relative_segments: list[str]) -> httpx.Response:
        return await client.post(f"{targets[0].backend_url}/api/internal/extension-files/read",
            headers={"X-Local-Token": token, "X-BoxTeam-Gateway-Id": gateway_id, "X-Request-ID": request_id},
            json={"protocol_version": 1, "gateway_id": gateway_id,
                  "backend_id": first.extension_hosts.backend_id, "profile_id": profile_id,
                  "extension_id": extension_id, "extension_key": extension_key,
                  "relative_segments": relative_segments, "current_profile_member": True})

    package_root = first.extension_hosts.management._directory / "extensions" / extension_key
    invalid_id = "invalid-extension-path-" + uuid4().hex
    rejected_path = await native_package_read(invalid_id, ["..", "outside.txt"])
    assert rejected_path.status_code == 403, rejected_path.text
    assert rejected_path.headers["X-Request-ID"] == rejected_path.json()["request_id"] == invalid_id
    assert rejected_path.json()["detail"]["code"] == "NoPermissions"

    large_package_file = package_root / "large.bin"
    large_package_file.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    try:
        oversized_id = "oversized-extension-file-" + uuid4().hex
        oversized_package = await native_package_read(oversized_id, ["large.bin"])
        assert oversized_package.status_code == 413, oversized_package.text
        assert oversized_package.headers["X-Request-ID"] == oversized_package.json()["request_id"] == oversized_id
        assert oversized_package.json()["detail"]["code"] == "EntryTooLarge"
    finally:
        large_package_file.unlink()

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
    _, workspace, backends = management_backends
    first = _backend(workspace / "a", backends).extension_hosts.management
    second = _backend(workspace / "b", backends).extension_hosts.management
    original = vsix_factory(workspace, "1.0.0", "v1-remains-readable")
    await first.install(original)
    current = await first.install(vsix_factory(workspace, "2.0.0", "v2-remains-readable"))
    await first.install(vsix_factory(workspace, "1.0.0", "unrelated", manifest_overrides={"name": "unrelated"}))
    peer = await second.install(vsix_factory(workspace, "9.0.0", "other-target"))
    profile_id, extension_id = current.installed.profile_id, current.installed.id
    directory = first._directory / "extensions"
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
    _, workspace, backends = management_backends
    first = _backend(workspace / "a", backends).extension_hosts.management
    second = _backend(workspace / "b", backends).extension_hosts.management
    base = await first.install(vsix_factory(workspace, "1.0.0", "dependency"))
    dependent = await first.install(vsix_factory(workspace, "1.0.0", "dependent", manifest_overrides={
        "name": "dependent", "extensionDependencies": [base.installed.id]}))
    peer = await second.install(vsix_factory(workspace, "2.0.0", "other-target"))
    directory = first._directory / "extensions"
    before = installation_snapshot(directory)
    with pytest.raises(ExtensionManagementError, match="depends on this"):
        await first.uninstall(profile_id=base.installed.profile_id, extension_id=base.installed.id, version="1.0.0")
    assert installation_snapshot(directory) == before
    assert await first.inventory() == dependent.inventory
    assert await second.inventory() == peer.inventory
    assert not first._failed and first.drained
    removed_dependent = await first.uninstall(profile_id=dependent.installed.profile_id,
        extension_id=dependent.installed.id, version=dependent.installed.version)
    assert len(removed_dependent.inventory.extensions) == 1
    removed_base = await first.uninstall(profile_id=base.installed.profile_id,
        extension_id=base.installed.id, version=base.installed.version)
    assert removed_base.inventory.extensions == [] and first.drained
    assert await second.inventory() == peer.inventory
