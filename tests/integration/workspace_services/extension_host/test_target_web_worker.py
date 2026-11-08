"""真实 Gateway 发布与原 iframe／Worker；UI 服务和浏览器文件存储为具名装具。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from starlette.staticfiles import StaticFiles

from app.api.extension_configuration import router as backend_configuration_router
from app.api.extension_host import router as backend_host_router
from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import TraceMiddleware
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import RemoteGatewayConnection
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.registry import WorkspaceTarget
from app.gateway.routes.users import router as users_router
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.extension_configuration import (
    router as gateway_configuration_router,
)
from app.main import workspace_http_exception_handler
from tests.integration.workspace_services.extension_host.test_extension_management import (
    ManagementBackends,
    _backend,
    management_backends,
    management_runtime,
)

# pytest 通过导入的原 fixture 注入；不复制真实管理进程及 Gateway 生命周期。
__all__ = ["management_backends", "management_runtime"]


@pytest.fixture
def browser_vsix() -> Callable[[Path, str], Path]:
    def create(directory: Path, version: str) -> Path:
        target = directory / f"browser-{version}.vsix"
        manifest = {"name": "target-browser", "publisher": "boxteam-probe", "version": version,
            "engines": {"vscode": "^1.0.0"}, "browser": "./dist/extension.js", "extensionKind": ["web"],
            "activationEvents": ["onCommand:boxteam.browser.inspect"],
            "contributes": {"commands": [{"command": "boxteam.browser.inspect", "title": "目标 Worker"}]}}
        code = """
const vscode = require('vscode');
exports.activate = async context => {
  const chunkUri = vscode.Uri.joinPath(context.extensionUri, 'dist', 'chunk.mjs');
  const chunk = await import(chunkUri.toString(true));
  context.subscriptions.push(vscode.commands.registerCommand('boxteam.browser.inspect', async value => {
    const before = { global: context.globalState.get('value', null), workspace: context.workspaceState.get('value', null) };
    if (value !== undefined) {
      await context.globalState.update('value', value);
      await context.workspaceState.update('value', value);
    }
    const errors = [];
    const unsupported = async namespace => Promise.all(Object.keys(namespace).map(async key => {
      try { const value = namespace[key]; await (typeof value === 'function' ? value() : value); return null; }
      catch (error) { return error.message; }
    }));
    const debugErrors = await unsupported(vscode.debug), taskErrors = await unsupported(vscode.tasks);
    let deprecatedTaskError;
    try { vscode.workspace.registerTaskProvider('probe', {}); } catch (error) { deprecatedTaskError = error.message; }
    const terminalErrors = [() => vscode.window.createTerminal({name:'wrong-window'}), () => vscode.window.terminals,
      () => vscode.window.onDidOpenTerminal(() => {}), () => vscode.env.shell, () => context.environmentVariableCollection]
      .map(operation => { try { operation(); return null; } catch (error) { return error.message; } });
    for (const operation of [() => context.secrets.get('key'), () => context.secrets.store('key', 'value'),
      () => context.secrets.delete('key'), () => context.secrets.keys(), () => context.globalState.setKeysForSync(['value'])]) {
      try { await operation(); } catch (error) { errors.push(error.message); }
    }
    return { version: context.extension.packageJSON.version, extensionUri: context.extensionUri.toString(true),
      chunk: chunk.marker, style: chunk.style, marker: await (await fetch(vscode.Uri.joinPath(context.extensionUri, 'dist', 'marker.txt').toString(true))).text(),
      folders: vscode.workspace.workspaceFolders?.map(folder => folder.uri.toString(true)) ?? [],
      setting: vscode.workspace.getConfiguration('probe').get('remote'), before, errors, terminalErrors, debugErrors, taskErrors, deprecatedTaskError,
      globalStorageUri: context.globalStorageUri.toString(true), storageUri: context.storageUri?.toString(true),
      logUri: context.logUri.toString(true) };
  }));
};
"""
        with ZipFile(target, "w") as archive:
            archive.writestr("extension/package.json", json.dumps(manifest))
            archive.writestr("extension/dist/extension.js", code)
            archive.writestr("extension/dist/chunk.mjs", "import {marker} from './helper.mjs'; export {marker}; export const style = new URL('./probe.css', import.meta.url).href;\n")
            archive.writestr("extension/dist/helper.mjs", f"export const marker = {json.dumps(version)};\n")
            archive.writestr("extension/dist/marker.txt", version)
            archive.writestr("extension/dist/probe.css", ".worker-asset { width:12px; height:12px; background-image:url('./images/icon.svg'); }\n")
            archive.writestr("extension/dist/images/icon.svg", '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12"><rect width="12" height="12" fill="red"/></svg>')
        return target
    return create


async def test_gateway_frozen_packages_activate_original_target_workers_without_node_runtime(
    management_backends: ManagementBackends, browser_vsix,
    network_scope: AsyncExitStack, serve_extension_application: Callable[[FastAPI], Awaitable[str]],
) -> None:
    case, domain = management_backends, management_backends.domains[0]
    domain.application.include_router(users_router)
    domain.application.include_router(gateway_configuration_router)
    domain.application.state.streaming_http_client = await network_scope.enter_async_context(httpx.AsyncClient(timeout=None, trust_env=False))
    routes: list[str] = []

    @domain.application.middleware("http")
    async def observe(request: Request, call_next):
        routes.append(request.url.path)
        return await call_next(request)

    targets = []
    for index, version in enumerate(("1.0.0", "2.0.0")):
        selected = case.domains[index]
        if index:
            selected.application.include_router(gateway_configuration_router)
            selected.application.state.streaming_http_client = domain.application.state.streaming_http_client
        workspace = case.workspace / f"worker-{index}"
        registry = _backend(workspace, case, domain=index)
        registry.extension_hosts.runtime = None
        application = FastAPI()
        application.add_middleware(TraceMiddleware)
        application.exception_handler(HTTPException)(workspace_http_exception_handler)
        application.include_router(backend_host_router)
        application.include_router(backend_configuration_router)
        application.state.workspace_container_registry = registry
        url = await serve_extension_application(application)
        [mount] = registry.mounted_workspaces.mounts
        selected.registry.upsert(WorkspaceTarget(gateway_id=selected.gateway_id, workspace_id=mount.workspace_id,
            name=workspace.name, root_path=str(workspace), backend_url=url, connection_kind="local",
            backend_id=registry.extension_hosts.backend_id), activate=False)
        installed = await registry.extension_hosts.management.install(browser_vsix(workspace, version))
        profile = installed.inventory.profile_id
        targets.append({"gatewayId": selected.gateway_id, "backendId": registry.extension_hosts.backend_id,
            "workspaceId": mount.workspace_id, "root": f"boxteam://workspace/{mount.workspace_id}/resources/files/root",
            "version": version, "profile": profile})
        if index:
            credential = FederationCredentialStore(storage_path=get_gateway_root() / "credentials/federation.json").issue(
                connection_id="worker-target", peer_gateway_id=domain.gateway_id)
            domain.registry.upsert_remote_gateway(RemoteGatewayConnection(
                connection_id=credential.connection_id, name="Worker target", host="127.0.0.1", port=22,
                username="fixture", private_key_path=None, ssh_config_host=None, remote_gateway_port=1,
                remote_gateway_id=selected.gateway_id, protocol_version=2, source_owner="manual"),
                runtime=WorkspaceRuntime(service_urls={"workspace_api": selected.url}))
            domain.registry.upsert(WorkspaceTarget(gateway_id=selected.gateway_id, workspace_id=mount.workspace_id,
                name=workspace.name, root_path=None, backend_url="", connection_kind="remote_gateway", owner="remote_projection",
                remote_gateway_connection_ids=(credential.connection_id,), remote_service_names=("workspace_api",)), activate=False)
            selected.application.state.federation_policy_store.publish({"rules": [
                {"capability": capability, "effect": "allow", "principal_ref": principal_ref_for_gateway(domain.gateway_id)}
                for capability in ("extension_host_manage", "extension_installation_manage", "configuration_read", "configuration_write")]})
    upgrade = browser_vsix(case.workspace / "worker-0", "3.0.0")
    spec = {"url": domain.url, "cookie": domain.headers["Cookie"], "targets": targets,
        "upgrade": {"workspace_id": targets[0]["workspaceId"], "source_uri": f"{targets[0]['root']}/{upgrade.name}"},
        "artifacts": str(case.context.artifacts_dir), "commit": case.runtime.commit, "version": case.runtime.vscode_version}
    spec_path = case.context.artifacts_dir / "browser-spec.json"
    spec_path.write_text(json.dumps(spec))
    static = case.context.artifacts_dir / "browser"
    static.mkdir()
    domain.application.mount("/worker-fixture", StaticFiles(directory=static, html=True))
    command = ["bun", "tests/integration/workspace_services/extension_host/target_web_worker.mjs", str(spec_path)]
    process = await asyncio.create_subprocess_exec(*command, cwd=case.context.project_root,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        async with asyncio.timeout(100):
            output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    (case.context.artifacts_dir / "browser.log").write_bytes(output)
    (case.context.artifacts_dir / "browser-command.json").write_text(json.dumps({"command": command, "exit_code": process.returncode, "routes": routes}, indent=2))
    assert process.returncode == 0, output.decode(errors="replace")
    assert not any(path.startswith("/api/gateway/extension-hosts/") for path in routes)
    assert any(path.startswith("/api/gateway/extension-packages/content/") for path in routes)
    assert "/api/gateway/extension-configurations/read" in routes
    assert "/api/gateway/auth/local-credential" in routes
    assert all(not (registry.extension_hosts._home / "state/backend/extension-hosts").exists() for registry in case.registries)
