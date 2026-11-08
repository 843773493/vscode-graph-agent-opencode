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
from zipfile import ZipFile

import commentjson
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
