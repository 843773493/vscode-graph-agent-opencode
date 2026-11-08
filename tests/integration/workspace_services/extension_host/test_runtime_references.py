"""原安装入口、真实 Node registry 与 Backend 退出共同结清版本引用。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest

from app.gateway.runtime.extension_management import GatewayExtensionManagement
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
    NativeHostReference,
)
from app.services.infrastructure.extension_host.owner import NodeHostConnection
from tests.integration.workspace_services.extension_host.test_extension_management import (
    _backend,
    management_backends,
    management_runtime,
)

__all__ = ["management_backends", "management_runtime"]


def _vsix(directory: Path, *, name: str, version: str) -> Path:
    target = directory / f"{name}-{version}.vsix"
    with ZipFile(target, "w") as archive:
        archive.writestr("extension/package.json", json.dumps({
            "publisher": "boxteam-probe", "name": name, "version": version,
            "main": "./extension.js", "engines": {"vscode": "^1.0.0"},
            "extensionKind": ["workspace"], "activationEvents": [],
        }))
        archive.writestr("extension/extension.js", """
const vscode = require('vscode');
const fs = require('node:fs');
exports.activate = context => {
  const unsupported = async namespace => Promise.all(Object.keys(namespace).map(async key => {
    try { const value = namespace[key]; await (typeof value === 'function' ? value() : value); return null; }
    catch (error) { return error.message; }
  }));
  context.subscriptions.push(vscode.commands.registerCommand('runtime.inspect', async () => ({
    version: fs.readFileSync(require('node:path').join(__dirname, 'lazy.txt'), 'utf8'),
    ids: vscode.extensions.all.map(extension => extension.id),
    hasManagementToken: process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN !== undefined,
    hasReferenceCapability: process.env.BOXTEAM_EXTENSION_HOST_REFERENCE !== undefined,
    debugErrors: await unsupported(vscode.debug),
    taskErrors: await unsupported(vscode.tasks),
    deprecatedTaskError: await (async () => {
      try { vscode.workspace.registerTaskProvider('probe', {}); return null; } catch (error) { return error.message; }
    })(),
    terminalErrors: [() => vscode.window.createTerminal('wrong-window'), () => vscode.window.createTerminal({name:'wrong-window'}),
      () => vscode.window.createTerminal({name:'pty', pty:{}}), () => vscode.window.terminals, () => vscode.window.activeTerminal,
      () => vscode.window.onDidWriteTerminalData(() => {}), () => vscode.env.shell, () => context.environmentVariableCollection]
      .map(operation => { try { operation(); return null; } catch (error) { return error.message; } }),
  })));
};
""")
        archive.writestr("extension/lazy.txt", version)
    return target


async def test_real_node_registry_retains_versions_across_delta_update_uninstall_and_gateway_handoff(
    management_backends,
) -> None:
    case = management_backends
    registry = _backend(case.workspace / "runtime-target", case)
    owner = registry.extension_hosts
    management = owner.management
    domain = case.domains[0]
    installed = await management.install(_vsix(case.workspace, name="runtime", version="1.0.0"))
    profile = installed.inventory.profile_id
    original = installed.inventory.extensions[0]
    connections: dict[str, NodeHostConnection] = {}
    settled = {label: asyncio.Event() for label in ("first", "second", "third")}
    handlers: list[asyncio.Task[None]] = []
    current, dynamic = original, None
    environment = await management.environment()
    service = environment.reference_service
    process_id = domain.owner.instance.process_id
    http = httpx.AsyncClient(trust_env=False, timeout=10)

    def entry(value):
        return {**value.model_dump(mode="json"), "profile_id": profile}

    async def retained(label: str, keys: list[str]) -> None:
        reference = owner._sessions[connections[label].identity.generation].reference
        assert reference is not None
        publication = await management.host_publication(*reference, profile_id=profile, extension_keys=keys)
        assert [value.extension_key for value in publication.publications] == keys

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal current, dynamic
        operation = (await reader.readline()).decode().strip()
        try:
            if operation in settled:
                async with owner.connect(gateway_id=domain.gateway_id, expected_backend_id=owner.backend_id,
                    client_id="runtime-references", view_id=operation, workspace_ids=registry.mounted_workspaces.workspace_ids) as connection:
                    connections[operation] = connection
                    writer.write(json.dumps({"initialization": connection.initialization.model_dump(mode="json"),
                        "extension": entry(current), "target": {"kind": "backend", "gatewayId": domain.gateway_id,
                            "backendId": owner.backend_id}}).encode() + b"\n")
                    await writer.drain()

                    async def forward(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
                        while data := await source.read(65536):
                            destination.write(data)
                            await destination.drain()

                    tasks = [asyncio.create_task(forward(reader, connection.writer)),
                        asyncio.create_task(forward(connection.reader, writer))]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        for outcome in results:
                            if isinstance(outcome, Exception):
                                raise outcome
                assert connection.host.child.state.exit_verified
                settled[operation].set()
                return
            result: dict[str, object]
            if operation == "update":
                updated = await management.install(_vsix(case.workspace, name="runtime", version="2.0.0"))
                current = next(value for value in updated.inventory.extensions if value.extension_key != original.extension_key)
                additional = await management.install(_vsix(case.workspace, name="dynamic", version="1.0.0"))
                dynamic = next(value for value in additional.inventory.extensions if value.extension_key not in {original.extension_key, current.extension_key})
                result = {"dynamic": entry(dynamic), "foreign_backend": case.domains[1].owner.backend_id}
            elif operation == "handoff-and-uninstall":
                previous = domain.owner
                await previous.close(preserve=True)
                replacement = GatewayExtensionManagement(backend_id=owner.backend_id, runtime=case.runtime,
                    directory=previous.directory, environment=os.environ,
                    log_dir=domain.home / "state/logs",
                    logging_startup={"revision": "runtime-reference-handoff", "level": "info", "pretty": False})
                case.owners.append(replacement)
                await replacement.start()
                domain.owner = replacement
                domain.application.state.extension_management = replacement
                assert replacement.instance is not None and replacement.instance.process_id == process_id
                assert (await management.environment()).reference_service == service
                await management.uninstall(profile_id=profile, extension_id="boxteam-probe.runtime", version="2.0.0")
                await management.uninstall(profile_id=profile, extension_id="boxteam-probe.dynamic", version="1.0.0")
                assert not (await management.inventory()).extensions
                result = {"same_process": True, "inventory": []}
            elif operation in {"assert-retained", "assert-second"}:
                assert dynamic is not None
                if operation == "assert-second":
                    await settled["first"].wait()
                    assert connections["first"].identity.generation not in owner._sessions
                else:
                    await retained("first", [original.extension_key, dynamic.extension_key])
                await retained("second", [original.extension_key])
                await retained("third", [current.extension_key])
                # 新宿主只能使用当前 profile，不能从在用旧包取得新准入。
                fresh = NativeHostReference(generation="fresh-after-uninstall", capability="new-owner")
                await management.host_reference(service, fresh, operation="open-host")
                try:
                    with pytest.raises(ExtensionManagementError):
                        await management.host_publication(service, fresh, profile_id=profile, extension_keys=[original.extension_key])
                finally:
                    await management.host_reference(service, fresh, operation="close-host")
                reference = owner._sessions[connections["second"].identity.generation].reference
                assert reference is not None
                # 同一运行能力不能打开／关闭引用或取得通用管理权限。
                for method in ("open-host", "close-host"):
                    denied = await http.post(service.url, json={"method": method, "backend_id": service.backend_id,
                        "runtime_instance_id": service.runtime_instance_id, **reference[1].model_dump()})
                    assert "error" in denied.json() and "result" not in denied.json()
                forbidden = await http.post(service.url.removesuffix("/references") + "/management", json={"method": "inventory"})
                assert forbidden.status_code == 401
                response = await http.post(service.url.removesuffix("/references") + "/shutdown",
                    headers={"X-Local-Token": case.token})
                assert response.status_code == 409 and response.json()["code"] == "RuntimeReferencesActive"
                domain.owner.assert_healthy()
                result = {"protected": True, "shutdown_status": response.status_code}
            elif operation == "assert-settled":
                await asyncio.gather(*(event.wait() for event in settled.values()))
                assert not owner.active and all(connection.host.child.state.exit_verified for connection in connections.values())
                await domain.owner.close()
                assert domain.owner.drained
                result = {"exited": True}
            else:
                raise AssertionError(operation)
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.append(asyncio.create_task(serve(reader, writer)))

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    configuration = case.context.artifacts_dir / "runtime-configuration.json"
    configuration.write_text(json.dumps({"port": server.sockets[0].getsockname()[1],
        "runtime": str(case.runtime.entrypoint.parent), "artifacts": str(case.context.artifacts_dir)}))
    command = [sys.executable, str(case.context.project_root / "app/runtime" / (
        "windows_job.py" if sys.platform == "win32" else "posix_job.py")),
        "--memory-limit-kb", "4194304", "--timeout-seconds", "60", "--", str(case.runtime.executable),
        "tests/integration/workspace_services/extension_host/runtime_references.mjs", str(configuration)]
    try:
        result = await asyncio.to_thread(subprocess.run, command, cwd=case.context.project_root,
            capture_output=True, timeout=70, check=False)
        (case.context.artifacts_dir / "runtime.stdout.raw").write_bytes(result.stdout)
        (case.context.artifacts_dir / "runtime.stderr.raw").write_bytes(result.stderr)
        (case.context.artifacts_dir / "runtime-process.json").write_text(json.dumps({
            "command": command, "exit_code": result.returncode,
            "hosts": {label: asdict(connection.host.child.state) for label, connection in connections.items()}}))
        assert result.returncode == 0, (result.stdout + result.stderr).decode(errors="replace")
    finally:
        server.close()
        await server.wait_closed()
        for handler in handlers:
            if not handler.done():
                handler.cancel()
        outcomes = await asyncio.gather(*handlers, return_exceptions=True)
        await http.aclose()
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                raise outcome
