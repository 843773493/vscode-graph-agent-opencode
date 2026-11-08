"""默认 Workbench 的目标 Web／Node 分派、声明式贡献与冷恢复；SSH 为具名装具。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname
from zipfile import ZipFile

import commentjson
import psutil
import pytest

from app.core.resource_uri import resource_display_uri
from app.gateway.federation.ports import principal_ref_for_gateway
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import TargetGatewayNetwork
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_network as target_gateway_network,  # noqa: PLC0414
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    management_runtime as management_runtime,  # noqa: PLC0414
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    vsix_factory as vsix_factory,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS


@pytest.fixture(autouse=True)
def empty_target_runtime(
    management_runtime, runtime_manifest_path: Path, web_workbench_assets: Path,
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, component, runtime = management_runtime
    manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
    manifest["extension_host"] = {"node_executable": str(runtime.executable), "component_root": str(component)}
    path = context.runtime_root_for_node(request.node.nodeid) / "empty-target-runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_RUNTIME_MANIFEST", str(path))
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))
    # 该网络装具独立启动Backend，仍需沿原Gateway本机凭据准入内部文件来源请求。
    monkeypatch.setenv("BOXTEAM_RUNTIME_CONTROL_TOKEN", LOCAL_TOKEN_HEADERS["X-Local-Token"])


async def test_default_empty_workbench_runs_entry_browser_target_after_management_switch(
    target_gateway_network: TargetGatewayNetwork, vsix_factory,
) -> None:
    network = target_gateway_network
    local, remote = network.clients
    gateway_a, gateway_b = network.gateway_ids
    context = network.context
    evidence: list[dict[str, object]] = []

    def exchange(client, method: str, path: str, body: dict[str, object] | None = None):
        response = client.request(method, path, json=body)
        payload = response.json()
        evidence.append({"method": method, "path": path, "status": response.status_code, "body": payload})
        assert response.is_success, response.text
        assert payload["request_id"] == response.headers["X-Request-ID"]
        return payload["data"]

    # 两域共用公开ID但版本不同，管理标题切B后若运行身份串绑会立即返回错误版本。
    source = exchange(remote, "GET", "/api/gateway/config/sources/gateway_mutable_override")
    config = commentjson.loads(source["jsonc"])
    config.setdefault("permissions", {})["federation"] = {"default_effect": "allow", "rules": [
        {"capability": capability, "effect": "allow", "principal_ref": principal_ref_for_gateway(gateway_a)}
        for capability in ("extension_installation_manage", "extension_host_manage")]}
    exchange(remote, "PUT", "/api/gateway/config/sources/gateway_mutable_override", {
        "gateway_id": gateway_b, "source_key": "gateway_mutable_override", "jsonc": json.dumps(config),
        "base_file_digest": source["file_digest"], "base_layer_revision": source["layer_revision"],
        "base_layer_digest": source["layer_digest"], "expected_active_revision": source["active_revision"],
        "expected_active_digest": source["active_digest"], "idempotency_key": "empty-target-web-policy"})
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    exchange(local, "POST", "/api/gateway/remote-gateways", {"name": "Empty Target B", "host": "127.0.0.1", "port": 22,
        "username": "empty-target", "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3]})

    targets = {}
    theme = {"label": "Empty Target Theme", "colors": {"a": "#123456", "b": "#654321"}}
    notebook_probe = """
function registerNotebookProbe(context, id) {
  context.subscriptions.push(vscode.commands.registerCommand(id, async () => {
    const rejected = [];
    for (const [namespace, api] of [['window', vscode.window], ['workspace', vscode.workspace], ['notebooks', vscode.notebooks]]) {
      for (const name of Object.keys(api).filter(name => namespace === 'notebooks' || /notebook/i.test(name)).sort()) {
        try {
          const member = api[name];
          await (typeof member === 'function' ? member() : member);
          rejected.push({ api: namespace + '.' + name, accepted: true });
        } catch (error) {
          rejected.push({ api: namespace + '.' + name, error: String(error) });
        }
      }
    }
    const cell = new vscode.NotebookCellData(vscode.NotebookCellKind.Code, 'print(1)', 'python');
    const data = new vscode.NotebookData([cell]);
    const edit = vscode.NotebookEdit.updateNotebookMetadata({ probe: true });
    return { rejected, values: { language: data.cells[0].languageId, count: data.cells.length, metadata: edit.newNotebookMetadata } };
  }));
}
"""
    browser_code = """
const vscode = require('vscode');
exports.activate = context => {
  registerNotebookProbe(context, 'boxteam.empty.webNotebooks');
  context.subscriptions.push(vscode.languages.registerWorkspaceSymbolProvider({
    provideWorkspaceSymbols(query) {
      const folder = vscode.workspace.workspaceFolders?.[0];
      if (query !== 'ScopedSymbolProbe' || !folder) return [];
      return [new vscode.SymbolInformation('ScopedSymbolProbe', vscode.SymbolKind.Function, '目标 Web 原 Provider',
        new vscode.Location(vscode.Uri.joinPath(folder.uri, 'scope-symbol.ts'), new vscode.Position(0, 0)))];
    }
  }));
  const activation = crypto.randomUUID();
  context.subscriptions.push(vscode.commands.registerCommand('boxteam.empty.runtimeDiagnostics',
    () => vscode.commands.executeCommand('boxteam.extensions.getRuntimeDiagnostics')));
  context.subscriptions.push(vscode.commands.registerCommand('boxteam.empty.inspect', value => ({
    value, activation, version: context.extension.packageJSON.version,
    folders: vscode.workspace.workspaceFolders?.map(folder => folder.uri.toString(true)) ?? [],
    extensionUri: context.extensionUri.toString(true),
    webWorker: typeof WorkerGlobalScope !== 'undefined' && globalThis instanceof WorkerGlobalScope,
    uiKind: vscode.env.uiKind
  })));
};
""" + notebook_probe
    for label, client, gateway_id, workspace, version in zip(
        ("a", "b"), network.clients, network.gateway_ids, network.workspace_roots, ("1.0.0", "2.0.0"), strict=True,
    ):
        (workspace / "scope-symbol.ts").write_text("export function ScopedSymbolProbe() {}\n", encoding="utf-8")
        directory = exchange(client, "POST", "/api/gateway/extension-targets", {"protocol_version": 1, "gateway_id": gateway_id})
        target, = directory["targets"]
        targets[label] = {"gateway_id": gateway_id, "backend_id": target["backend_id"]}
        package = vsix_factory(workspace, version, "empty-browser", manifest_overrides={
            "name": "empty-browser", "displayName": "空根目标 Web", "browser": "./browser.js", "extensionKind": ["web"],
            "activationEvents": ["onCommand:boxteam.empty.inspect"],
            "contributes": {"commands": [{"command": "boxteam.empty.inspect", "title": "空根目标检查"}]},
        }, omit=("main",))
        with ZipFile(package, "a") as archive:
            archive.writestr("extension/browser.js", browser_code)
        install = exchange(client, "POST", "/api/gateway/extension-installations/install", {
            "protocol_version": 1, **targets[label], "workspace_ids": [network.workspace_id],
            "source_uri": resource_display_uri(scope="workspace", scope_id=network.workspace_id, kind="files",
                tail_segments=("root", package.name), gateway_authority=gateway_id)})
        assert install["installed"]["id"] == "boxteam-probe.empty-browser"
        assert install["installed"]["version"] == version
        # 主题不声明main、browser或kind，必须经原manifest推导和冻结发布进入窗口贡献。
        theme_package = vsix_factory(workspace, version, "纯声明式主题", manifest_overrides={
            "name": "empty-theme", "displayName": theme["label"],
            "contributes": {"themes": [{"id": "boxteam.empty.theme", "label": theme["label"],
                "uiTheme": "vs-dark", "path": "./theme.json"}]},
        }, omit=("main", "extensionKind", "activationEvents"))
        with ZipFile(theme_package, "a") as archive:
            archive.writestr("extension/theme.json", json.dumps({
                "colors": {"editor.background": theme["colors"][label]}, "tokenColors": []}))
        installed_theme = exchange(client, "POST", "/api/gateway/extension-installations/install", {
            "protocol_version": 1, **targets[label], "workspace_ids": [network.workspace_id],
            "source_uri": resource_display_uri(scope="workspace", scope_id=network.workspace_id, kind="files",
                tail_segments=("root", theme_package.name), gateway_authority=gateway_id)})
        assert installed_theme["installed"]["id"] == "boxteam-probe.empty-theme"
        assert installed_theme["installed"]["version"] == version
        entry, = [entry for entry in installed_theme["inventory"]["extensions"] if entry["description"]["name"] == "empty-theme"]
        assert entry["is_valid"] and not entry["description"].get("main") and not entry["description"].get("browser")

        hybrid = vsix_factory(workspace, version, "双入口", manifest_overrides={
            "name": "empty-hybrid", "main": "./node.js", "browser": "./browser.js",
            "extensionKind": ["workspace", "web"], "activationEvents": ["onCommand:boxteam.empty.hybrid"],
            "contributes": {"commands": [{"command": "boxteam.empty.hybrid", "title": "双入口检查"}]},
        })
        with ZipFile(hybrid, "a") as archive:
            archive.writestr("extension/browser.js", """
const vscode = require('vscode');
exports.activate = context => context.subscriptions.push(vscode.commands.registerCommand('boxteam.empty.hybrid', () => ({
  mode: 'web', version: context.extension.packageJSON.version, worker: typeof document === 'undefined' && typeof self !== 'undefined',
  extensionUri: context.extensionUri.toString(true), folders: vscode.workspace.workspaceFolders?.map(folder => folder.uri.toString(true)) ?? [],
})));
""")
            archive.writestr("extension/node.js", """
const vscode = require('vscode');
exports.activate = async context => {
const activation = context.globalState.get('crashActivations', 0) + 1;
await context.globalState.update('crashActivations', activation);
registerNotebookProbe(context, 'boxteam.empty.nodeNotebooks');
// 原宿主拦截 process.exit；信号只终止本扩展宿主，制造真实进程崩溃。
context.subscriptions.push(vscode.commands.registerCommand('boxteam.empty.crashNode', () => process.kill(process.pid, 'SIGKILL')));
context.subscriptions.push(vscode.languages.registerWorkspaceSymbolProvider({
  async provideWorkspaceSymbols(query) {
    if (query === 'ScopedSymbolProbe') throw new Error('目标 Node 符号故障探针');
    return [];
  }
}));
context.subscriptions.push(vscode.commands.registerCommand('boxteam.empty.hybrid', () => ({
  mode: 'node', version: context.extension.packageJSON.version, platform: process.platform, pid: process.pid, activation,
  nativeContent: require('node:fs').readFileSync(require('node:path').join(__dirname, 'native.txt'), 'utf8'),
  folders: vscode.workspace.workspaceFolders?.map(folder => folder.uri.toString(true)) ?? [],
})), vscode.commands.registerCommand('boxteam.empty.nodeDiagnostics',
  () => vscode.commands.executeCommand('boxteam.extensions.getRuntimeDiagnostics')));
};
""" + notebook_probe)
            archive.writestr("extension/native.txt", "hybrid-node-" + version)
        installed_hybrid = exchange(client, "POST", "/api/gateway/extension-installations/install", {
            "protocol_version": 1, **targets[label], "workspace_ids": [network.workspace_id],
            "source_uri": resource_display_uri(scope="workspace", scope_id=network.workspace_id, kind="files",
                tail_segments=("root", hybrid.name), gateway_authority=gateway_id)})
        assert installed_hybrid["installed"]["id"] == "boxteam-probe.empty-hybrid"
        assert installed_hybrid["installed"]["version"] == version

    node = vsix_factory(network.workspace_roots[0], "1.0.0", "不得在空根窗口激活", manifest_overrides={
        "name": "empty-node-only", "main": "./node-only.js", "activationEvents": ["onCommand:boxteam.empty.nodeOnly"],
        "contributes": {"commands": [{"command": "boxteam.empty.nodeOnly", "title": "只允许挂根的 Node"}]},
    })
    with ZipFile(node, "a") as archive:
        archive.writestr("extension/node-only.js", """
const vscode = require('vscode');
exports.activate = context => context.subscriptions.push(vscode.commands.registerCommand('boxteam.empty.nodeOnly', () => ({
  mode: 'node', version: context.extension.packageJSON.version,
})));
""")
    exchange(local, "POST", "/api/gateway/extension-installations/install", {
        "protocol_version": 1, **targets["a"], "workspace_ids": [network.workspace_id],
        "source_uri": resource_display_uri(scope="workspace", scope_id=network.workspace_id, kind="files",
            tail_segments=("root", node.name), gateway_authority=gateway_a)})
    (context.artifacts_dir / "preparation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "空根目标 Web 验收需要真实 Chromium"
    output = context.artifacts_dir / "empty-target-browser.json"
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "150", "--",
        "bun", "tests/integration/clients/web/empty_target_web.mjs"]
    paths = ["app/gateway/server/extension_host.py", "src/clients/workbench/src/extensions/inventory.ts", "src/clients/workbench/scripts/extensions/extensionBrowserTransform.mjs",
        "src/clients/workbench/src/extensions/webHost.ts", "src/clients/workbench/src/extensions/targetContext.ts",
        "src/clients/workbench/src/extensions/runtimeDiagnostics.ts", "src/clients/workbench/src/extensions/commands.ts",
        "src/clients/workbench/src/extensions/contribution.ts",
        "src/clients/workbench/src/extensions/targetUris.ts", "src/clients/workbench/scripts/sourceTransform.mjs",
        "src/clients/workbench/scripts/extensions/extensionNotebookTransform.mjs",
        "src/clients/workbench/scripts/extensions/extensionOwnerTransform.mjs",
        "src/clients/workbench/scripts/extensions/extensionRunningTransform.mjs",
        "src/clients/workbench/scripts/extensions/extensionWorkspaceSymbolsTransform.mjs",
        "src/clients/workbench/src/search/symbolsQuickAccess.ts",
        "src/clients/workbench/scripts/extensions/extensionEnablementTransform.mjs",
        "src/clients/workbench/scripts/extensions/extensionStatusTransform.mjs", "tests/integration/clients/web/test_empty_target_web.py",
        "tests/integration/clients/web/empty_target_web.mjs"]
    fingerprints = {path: hashlib.sha256((Path.cwd() / path).read_bytes()).hexdigest() for path in paths}
    runtime = context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"])
    with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, cwd=Path.cwd(), capture_output=True, text=True, timeout=165, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_BROWSER_RESULT_PATH": str(output), "BOXTEAM_EMPTY_TARGET_WEB": json.dumps({
                    "base_url": str(local.base_url).rstrip("/"), "base_url_b": str(remote.base_url).rstrip("/"),
                    "targets": targets, "theme": theme, "workspace_id": network.workspace_id,
                    "native_root_a": network.workspace_roots[0].as_uri(), "native_platform": sys.platform})})
    (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    after = {path: hashlib.sha256((Path.cwd() / path).read_bytes()).hexdigest() for path in paths}
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode,
        "before": fingerprints, "after": after}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert fingerprints == after
    assert result.returncode == 0, result.stdout + result.stderr
    browser = json.loads(output.read_text(encoding="utf-8"))
    assert browser["phase"] == "complete" and browser["pageErrors"] == []
    assert browser["before"]["folders"] == browser["after"]["folders"] == []
    assert browser["before"]["version"] == browser["after"]["version"] == "1.0.0"
    assert browser["reloaded"]["version"] == "1.0.0" and browser["reloaded"]["folders"] == []
    assert browser["theme"] == dict.fromkeys(("selected", "afterManagementSwitch", "restored"), theme["colors"]["a"])
    assert browser["secondClient"]["inspection"]["version"] == "2.0.0" and browser["secondClient"]["inspection"]["folders"] == []
    assert browser["secondClient"]["themeColor"] == theme["colors"]["b"] and browser["secondClient"]["pageErrors"] == []
    assert browser["afterSecondClient"]["version"] == "1.0.0" and browser["afterSecondClient"]["folders"] == []
    assert browser["nodeChannels"] == browser["nodeResolves"] == []
    assert browser["secondClient"]["nodeChannels"] == browser["secondClient"]["nodeResolves"] == []
    assert browser["hybrid"]["mode"] == browser["secondClient"]["hybrid"]["mode"] == "web"
    assert browser["cacheRecovery"]["inspection"]["version"] == "1.0.0"
    assert browser["cacheRecovery"]["nodeChannels"] == browser["cacheRecovery"]["nodeResolves"] == []
    assert browser["attached"]["hybrid"]["mode"] == browser["attached"]["nodeOnly"]["mode"] == "node"
    assert browser["attached"]["hybrid"]["nativeContent"] == "hybrid-node-1.0.0"
    observations = [json.loads(line) for path in network.observation_paths.values() for line in path.read_text(encoding="utf-8").splitlines()]
    assert any(item["path"].endswith("/extension-hosts/resolve") for item in observations)


async def test_settings_ui_and_target_browser_share_native_configuration(
    target_gateway_network: TargetGatewayNetwork, vsix_factory,
) -> None:
    network = target_gateway_network
    local, remote = network.clients
    gateway_a, gateway_b = network.gateway_ids
    context = network.context
    receipts: list[dict[str, object]] = []

    def exchange(client, method: str, path: str, body: dict[str, object] | None = None):
        response = client.request(method, path, json=body)
        payload = response.json()
        receipts.append({"method": method, "path": path, "status": response.status_code, "body": payload})
        assert response.is_success, response.text
        assert payload["request_id"] == response.headers["X-Request-ID"]
        return payload["data"]

    source = exchange(remote, "GET", "/api/gateway/config/sources/gateway_mutable_override")
    config = commentjson.loads(source["jsonc"])
    config.setdefault("permissions", {})["federation"] = {"default_effect": "allow", "rules": [
        {"capability": capability, "effect": "allow", "principal_ref": principal_ref_for_gateway(gateway_a)}
        for capability in ("extension_installation_manage", "extension_host_manage", "configuration_read", "configuration_write")]}
    exchange(remote, "PUT", "/api/gateway/config/sources/gateway_mutable_override", {
        "gateway_id": gateway_b, "source_key": "gateway_mutable_override", "jsonc": json.dumps(config),
        "base_file_digest": source["file_digest"], "base_layer_revision": source["layer_revision"],
        "base_layer_digest": source["layer_digest"], "expected_active_revision": source["active_revision"],
        "expected_active_digest": source["active_digest"], "idempotency_key": "settings-browser-policy"})
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    exchange(local, "POST", "/api/gateway/remote-gateways", {"name": "Settings Target B", "host": "127.0.0.1", "port": 22,
        "username": "settings-target", "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3]})

    targets = {}
    originals = [(root / ".boxteam/workspace.jsonc").read_bytes() for root in network.workspace_roots]
    for label, client, gateway_id, workspace in zip(("a", "b"), network.clients, network.gateway_ids, network.workspace_roots, strict=True):
        directory = exchange(client, "POST", "/api/gateway/extension-targets", {"protocol_version": 1, "gateway_id": gateway_id})
        target, = directory["targets"]
        targets[label] = {"gateway_id": gateway_id, "backend_id": target["backend_id"]}
        package = vsix_factory(workspace, "1.0.0", "settings-browser", manifest_overrides={
            "name": "settings-browser", "displayName": "目标配置浏览器扩展", "browser": "./browser.js", "extensionKind": ["web"],
            "activationEvents": ["onCommand:boxteam.settingsProbe.read"], "contributes": {
                "commands": [{"command": "boxteam.settingsProbe.read", "title": "读取目标配置"}],
                "configuration": {"title": "目标配置", "properties": {
                    "targetSettingsProbe.value": {"type": "string", "default": "target-default", "scope": "window"}}}}}, omit=("main",))
        with ZipFile(package, "a") as archive:
            archive.writestr("extension/browser.js", """
const vscode = require('vscode');
exports.activate = context => {
  const events = [];
  const read = () => ({ value: vscode.workspace.getConfiguration('targetSettingsProbe').get('value'),
    inspect: vscode.workspace.getConfiguration('targetSettingsProbe').inspect('value'), events: [...events],
    extensionUri: context.extensionUri.toString(true), worker: typeof WorkerGlobalScope !== 'undefined' && self instanceof WorkerGlobalScope });
  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration(event => {
    if (event.affectsConfiguration('targetSettingsProbe.value')) events.push(read().value);
  }), vscode.commands.registerCommand('boxteam.settingsProbe.read', read),
  vscode.commands.registerCommand('boxteam.settingsProbe.update', async value => {
    await vscode.workspace.getConfiguration('targetSettingsProbe').update('value', value, vscode.ConfigurationTarget.Global);
    return read();
  }));
};
""")
        installed = exchange(client, "POST", "/api/gateway/extension-installations/install", {
            "protocol_version": 1, **targets[label], "workspace_ids": [network.workspace_id],
            "source_uri": resource_display_uri(scope="workspace", scope_id=network.workspace_id, kind="files",
                tail_segments=("root", package.name), gateway_authority=gateway_id)})
        assert installed["installed"]["id"] == "boxteam-probe.settings-browser"
    (context.artifacts_dir / "preparation.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding="utf-8")

    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "目标设置联合验收需要真实 Chromium"
    output = context.artifacts_dir / "target-settings-browser.json"
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
        "bun", "tests/integration/clients/web/target_settings_federation.mjs"]
    runtime = context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"])
    with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=195, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_BROWSER_RESULT_PATH": str(output), "BOXTEAM_TARGET_SETTINGS": json.dumps({
                    "base_url": str(local.base_url).rstrip("/"), "base_url_b": str(remote.base_url).rstrip("/"), "targets": targets})})
    (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}, indent=2), encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    browser = json.loads(output.read_text(encoding="utf-8"))
    assert browser["phase"] == "complete"
    assert browser["a"]["pageErrors"] == browser["b"]["pageErrors"] == []
    assert browser["a"]["nodeChannels"] == browser["b"]["nodeChannels"] == []
    assert browser["a"]["nodeResolves"] == browser["b"]["nodeResolves"] == []
    assert browser["binding"]["gateway_id"] == gateway_b
    settings_uri = urlsplit(browser["binding"]["context"]["remote_settings"]["native_uri"])
    assert settings_uri.scheme == "file" and not settings_uri.netloc
    native = Path(url2pathname(settings_uri.path)).resolve()
    expected = network.gateway_homes[1] / "state/backend/extension-management" / targets["b"]["backend_id"] / "data/User/settings.json"
    assert native == expected.resolve(), "设置正文必须属于 B 原安装器的默认 profile"
    saved = native.read_text(encoding="utf-8")
    assert commentjson.loads(saved) == {"targetSettingsProbe.value": "from-B-worker"}
    assert all((root / ".boxteam/workspace.jsonc").read_bytes() == original
        for root, original in zip(network.workspace_roots, originals, strict=True))
    assert browser["localValue"] == "target-default"
    assert browser["remoteValue"] == browser["uiValue"] == "from-B-worker"
    assert browser["a"]["writes"] and all(write["gateway_id"] == gateway_b and write["backend_id"] == targets["b"]["backend_id"]
        and "generation" not in write for write in browser["a"]["writes"])


async def test_target_node_tabgroups_multidiff_move_and_scoped_close(
    target_gateway_network: TargetGatewayNetwork, vsix_factory,
) -> None:
    network = target_gateway_network
    local, remote = network.clients
    gateway_a, gateway_b = network.gateway_ids
    context = network.context
    receipts: list[dict[str, object]] = []

    def exchange(client, method: str, path: str, body: dict[str, object] | None = None):
        response = client.request(method, path, json=body)
        payload = response.json()
        receipts.append({"method": method, "path": path, "url": str(response.request.url), "request": body,
            "status": response.status_code, "header_request_id": response.headers.get("X-Request-ID"), "body": payload})
        with (context.artifacts_dir / "tabs-http-receipts.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(receipts[-1], ensure_ascii=False) + "\n")
        assert response.is_success, response.text
        assert payload["request_id"] == response.headers["X-Request-ID"]
        return payload["data"]

    source = exchange(remote, "GET", "/api/gateway/config/sources/gateway_mutable_override")
    config = commentjson.loads(source["jsonc"])
    config.setdefault("permissions", {})["federation"] = {"default_effect": "allow", "rules": [
        {"capability": capability, "effect": "allow", "principal_ref": principal_ref_for_gateway(gateway_a)}
        for capability in ("extension_installation_manage", "extension_host_manage", "configuration_read")]}
    exchange(remote, "PUT", "/api/gateway/config/sources/gateway_mutable_override", {
        "gateway_id": gateway_b, "source_key": "gateway_mutable_override", "jsonc": json.dumps(config),
        "base_file_digest": source["file_digest"], "base_layer_revision": source["layer_revision"],
        "base_layer_digest": source["layer_digest"], "expected_active_revision": source["active_revision"],
        "expected_active_digest": source["active_digest"], "idempotency_key": "tabs-product-policy"})
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    registered = exchange(local, "POST", "/api/gateway/remote-gateways", {"name": "Tabs Target B", "host": "127.0.0.1", "port": 22,
        "username": "tabs-target", "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3]})
    connection_id, = [connection["gateway_connection_id"] for item in registered["items"] if item["gateway_id"] == gateway_b
        for connection in item["remote_gateways"] if connection["gateway_id"] == gateway_b]
    targets, services, installations, management_directories = {}, {}, {}, {}
    identity_fields = ("backend_id", "process_id", "runtime_instance_id", "base_url", "commit", "vscode_version", "node_version")
    for label, client, gateway_id, workspace, home in zip(("a", "b"), network.clients, network.gateway_ids,
        network.workspace_roots, network.gateway_homes, strict=True):
        directory = exchange(client, "POST", "/api/gateway/extension-targets", {"protocol_version": 1, "gateway_id": gateway_id})
        target, = directory["targets"]
        targets[label] = {"gateway_id": gateway_id, "backend_id": target["backend_id"]}
        package = vsix_factory(workspace, "1.0.0", "tabs-product", manifest_overrides={
            "name": "tabs-" + label, "displayName": "真实目标标签 " + label.upper(), "main": "./tabs.js",
            "activationEvents": ["onCommand:boxteam.tabs." + label + ".read"],
            "contributes": {"commands": [{"command": "boxteam.tabs." + label + ".read", "title": "读取目标标签 " + label.upper()}]},
        })
        with ZipFile(package, "a") as archive:
            archive.writestr("extension/tabs.js", "const label = " + json.dumps(label) + ";\n" + r"""
const vscode = require('vscode');
exports.activate = context => {
  const ids = new WeakMap(), events = [];
  let next = 0;
  const id = value => { if (!ids.has(value)) ids.set(value, ++next); return ids.get(value); };
  const input = value => value instanceof vscode.TabInputTextMultiDiff
    ? { kind: 'multiDiff', textDiffs: value.textDiffs.map(pair => ({ original: pair.original.toString(true), modified: pair.modified.toString(true) })) }
    : value instanceof vscode.TabInputText ? { kind: 'text', uri: value.uri.toString(true) } : { kind: 'other' };
  const tab = value => ({ identity: id(value), label: value.label, active: value.isActive, dirty: value.isDirty, input: input(value.input) });
  const group = value => ({ identity: id(value), column: value.viewColumn, active: value.isActive,
    activeTab: value.activeTab ? value.activeTab.label : null, tabs: value.tabs.map(tab) });
  const read = () => ({ label, pid: process.pid, node: process.versions.node, extensionId: context.extension.id,
    folders: vscode.workspace.workspaceFolders.map(folder => folder.uri.toString(true)),
    groups: vscode.window.tabGroups.all.map(group), events: [...events] });
  const register = (name, callback) => context.subscriptions.push(vscode.commands.registerCommand('boxteam.tabs.' + label + '.' + name, callback));
  context.subscriptions.push(vscode.window.tabGroups.onDidChangeTabs(event => events.push({ kind: 'tabs',
    opened: event.opened.map(tab), closed: event.closed.map(tab), changed: event.changed.map(tab) })),
  vscode.window.tabGroups.onDidChangeTabGroups(event => events.push({ kind: 'groups',
    opened: event.opened.map(group), closed: event.closed.map(group), changed: event.changed.map(group) })));
  register('read', read);
  const resource = name => vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'tabs-' + label + '-' + name + '.ts');
  register('prepare', async () => {
    for (const name of ['first', 'second', 'old1', 'new1', 'old2', 'new2']) {
      await vscode.workspace.fs.writeFile(resource(name), Buffer.from('export const value = ' + JSON.stringify(label + '-' + name) + ';\n'));
    }
    return read();
  });
  register('open', async ({ name, column = 1 }) => {
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(resource(name)), { viewColumn: column, preview: false });
    return read();
  });
  register('changes', async () => {
    await vscode.commands.executeCommand('vscode.changes', 'Tabs Own ' + label.toUpperCase(),
      [1, 2].map(index => [resource('new' + index), resource('old' + index), resource('new' + index)]));
  });
  register('close', async ({ name, wholeGroup = false }) => {
    const owner = vscode.window.tabGroups.all.find(group => group.tabs.some(tab => tab.label === name));
    if (!owner) throw new Error('目标标签不存在：' + name);
    const target = wholeGroup ? owner : owner.tabs.find(tab => tab.label === name);
    return { closed: await vscode.window.tabGroups.close(target, true), snapshot: read() };
  });
};
""")
        installed = exchange(client, "POST", "/api/gateway/extension-installations/install", {
            "protocol_version": 1, **targets[label], "workspace_ids": [network.workspace_id],
            "source_uri": resource_display_uri(scope="workspace", scope_id=network.workspace_id, kind="files",
                tail_segments=("root", package.name), gateway_authority=gateway_id)})
        assert installed["installed"]["id"] == "boxteam-probe.tabs-" + label
        installations[label] = installed["inventory"]
        directory = home / "state/backend/extension-management" / target["backend_id"]
        management_directories[label] = directory
        record = json.loads((directory / "service.json").read_text(encoding="utf-8"))
        process = psutil.Process(record["process_id"])
        assert record["backend_id"] == target["backend_id"] and process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        services[label] = {"identity": {key: record[key] for key in identity_fields}, "created_at": process.create_time()}
        extension, = installed["inventory"]["extensions"]
        package_directory = directory / "extensions" / extension["extension_key"]
        assert Path(extension["description"]["extensionLocation"]["fsPath"]).resolve() == package_directory.resolve()
        assert json.loads((package_directory / "package.json").read_text(encoding="utf-8"))["name"] == "tabs-" + label
    (context.artifacts_dir / "tabs-preparation.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (context.artifacts_dir / "tabs-management-before-browser.json").write_text(json.dumps({"services": services,
        "inventories": installations, "gateway_connection_id": connection_id}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "目标 TabGroups 验收需要真实 Chromium"
    output = context.artifacts_dir / "target-tabs-browser.json"
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "150", "--",
        "bun", "tests/integration/clients/web/target_tabs_product.mjs"]
    paths = ["tests/integration/clients/web/test_empty_target_web.py", "tests/integration/clients/web/target_tabs_product.mjs",
        "src/clients/workbench/scripts/extensions/extensionTabsTransform.mjs", "src/clients/workbench/src/extensions/targetUris.ts",
        "src/clients/workbench/src/extensions/targetContext.ts", "src/clients/workbench/scripts/sourceTransform.mjs"]
    before = {path: hashlib.sha256((Path.cwd() / path).read_bytes()).hexdigest() for path in paths}
    runtime = context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"])
    with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, cwd=Path.cwd(), capture_output=True, text=True, timeout=165, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_BROWSER_RESULT_PATH": str(output), "BOXTEAM_TABS_PRODUCT": json.dumps({
                    "base_url": str(local.base_url).rstrip("/"), "targets": targets, "workspace_id": network.workspace_id,
                    "native_roots": {label: root.as_uri() for label, root in zip(("a", "b"), network.workspace_roots, strict=True)}})})
    (context.artifacts_dir / "tabs-browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (context.artifacts_dir / "tabs-browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    after = {path: hashlib.sha256((Path.cwd() / path).read_bytes()).hexdigest() for path in paths}
    (context.artifacts_dir / "tabs-browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode,
        "before": before, "after": after}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert before == after
    assert result.returncode == 0, result.stdout + result.stderr
    browser = json.loads(output.read_text(encoding="utf-8"))
    assert browser["phase"] == "complete" and browser["pageErrors"] == []
    assert len(browser["bindings"]) == 2 and {binding["gateway_id"] for binding in browser["bindings"]} == {gateway_a, gateway_b}
    assert all(binding["workspace_ids"] == [network.workspace_id] for binding in browser["bindings"])
    assert browser["mixedRetained"] and browser["foreignRetained"] and browser["identityPreserved"]
    assert browser["cleanupCompleted"] is True

    after_inventory, after_services = {}, {}
    for label, client in zip(("a", "b"), network.clients, strict=True):
        current = exchange(client, "POST", "/api/gateway/extension-installations/inventory", {"protocol_version": 1, **targets[label]})
        assert current["gateway_id"] == targets[label]["gateway_id"] and current["backend_id"] == targets[label]["backend_id"]
        assert current["inventory"] == installations[label]
        assert [entry["description"]["id"] for entry in current["inventory"]["extensions"]] == ["boxteam-probe.tabs-" + label]
        after_inventory[label] = current
    relayed = exchange(local, "POST", "/api/gateway/extension-installations/inventory", {
        "protocol_version": 1, **targets["b"], "gateway_connection_id": connection_id})
    assert relayed == after_inventory["b"] and relayed != after_inventory["a"]
    for label, directory in management_directories.items():
        record = json.loads((directory / "service.json").read_text(encoding="utf-8"))
        process = psutil.Process(record["process_id"])
        after_services[label] = {"identity": {key: record[key] for key in identity_fields}, "created_at": process.create_time()}
        assert process.is_running() and process.status() != psutil.STATUS_ZOMBIE and after_services[label] == services[label]
        assert (directory / "extensions" / ("boxteam-probe.tabs-" + label + "-1.0.0") / "package.json").is_file()
        foreign = "b" if label == "a" else "a"
        assert not (directory / "extensions" / ("boxteam-probe.tabs-" + foreign + "-1.0.0")).exists()
    (context.artifacts_dir / "tabs-management-after-browser.json").write_text(json.dumps({
        "browser_cleanup_completed": browser["cleanupCompleted"], "original_services": services, "services_after": after_services,
        "direct_inventory": after_inventory, "relayed_inventory": relayed, "gateway_connection_id": connection_id,
        "boundary": "完整页面及Chromium沿原TestRunContext关闭后，原Gateway公开库存经原组件提供；A显式Relay读取B并核各原包目录。SSH为具名loopback装具。",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
