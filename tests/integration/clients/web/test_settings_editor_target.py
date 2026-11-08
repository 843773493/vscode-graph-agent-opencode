"""原设置编辑器经过真实 Gateway 保存目标 profile，JSON 和重开共用文件 owner。"""

from __future__ import annotations

import asyncio
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
import pytest

from app.core.resource_uri import resource_display_uri
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    InstallationCase,
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    installation_case as installation_case,  # noqa: PLC0414
)
from tests.integration.test_shared_managed_backend_lifecycle import (
    SharedBackendCase,
    _join_group,
)
from tests.integration.test_shared_managed_backend_lifecycle import (
    shared_backend_case as shared_backend_case,  # noqa: PLC0414
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    management_runtime as management_runtime,  # noqa: PLC0414
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    vsix_factory as vsix_factory,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(autouse=True)
def settings_management_component(management_runtime, runtime_manifest_path: Path, web_workbench_assets: Path,
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    context, component, runtime = management_runtime
    manifest = context.runtime_root_for_node(request.node.nodeid) / "settings-runtime.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
    payload["extension_host"] = {
        "node_executable": str(runtime.executable), "component_root": str(component),
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_TEST_MCP_RUNTIME_MANIFEST", str(manifest))
    monkeypatch.setenv("BOXTEAM_RUNTIME_MANIFEST", str(manifest))
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))


async def test_settings_editor_saves_target_json_and_restores_same_profile(installation_case: InstallationCase, vsix_factory) -> None:
    case = installation_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "设置编辑器验收需要真实 Chromium"
    workspace_config = (case.workspace / ".boxteam/workspace.jsonc").read_bytes()
    vsix = vsix_factory(case.workspace, "1.0.0", "settings-only", manifest_overrides={
        "contributes": {"configuration": {"title": "独立安装设置", "properties": {
            "schemaProbe.independent": {"type": "string", "default": "schema-default", "scope": "window",
                "description": "无需运行扩展宿主即可发现与保存"}}},
            "configurationDefaults": {"editor.tabSize": 3, "[typescript]": {"editor.tabSize": 5}}},
    })
    listing = case.client.get("/api/gateway/workspaces")
    listing.raise_for_status()
    target = listing.json()["data"]["active_workspace"]
    directory = case.client.post("/api/gateway/extension-configurations/targets", json={"protocol_version": 1, "gateway_id": target["gateway_id"]})
    directory.raise_for_status()
    backend_id, = directory.json()["data"]["backend_ids"]
    install = {"protocol_version": 1, "gateway_id": target["gateway_id"], "backend_id": backend_id,
        "workspace_ids": [target["workspace_id"]], "source_uri": resource_display_uri(scope="workspace",
            scope_id=target["workspace_id"], kind="files", tail_segments=("root", vsix.name), gateway_authority=target["gateway_id"])}
    result_path = case.context.artifacts_dir / "settings-editor-browser.json"
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "120", "--",
        "bun", "tests/integration/clients/web/settings_editor_target.mjs"]
    with chromium_temporary_environment(case.context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"]) / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=135, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_BROWSER_SETTINGS_INSTALL": json.dumps(install),
                "BOXTEAM_BROWSER_BASE_URL": str(case.client.base_url).rstrip("/"), "BOXTEAM_BROWSER_RESULT_PATH": str(result_path)})
    (case.context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (case.context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": result.returncode,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    browser = json.loads(result_path.read_text(encoding="utf-8"))
    assert browser["phase"] == "complete" and browser["pageErrors"] == []
    assert browser["hostChannels"] == [], "独立设置不需要扩展宿主"
    assert browser["writes"] and all("generation" not in item for item in browser["writes"])
    assert browser["jsonSavedAfterClosingSettingsUI"] and browser["jsonReturnedToTargetUI"] and browser["restoredTarget"]
    assert browser["independentSchemaSaved"]
    binding = browser["binding"]
    uri = urlsplit(binding["context"]["remote_settings"]["native_uri"])
    assert uri.scheme == "file" and not uri.netloc
    native = Path(url2pathname(uri.path)).resolve()
    assert native.is_relative_to(case.home.resolve())
    assert commentjson.loads(native.read_text(encoding="utf-8")) == {"http.proxy": "http://target.invalid:8080", "http.proxyStrictSSL": False}
    assert (case.workspace / ".boxteam/workspace.jsonc").read_bytes() == workspace_config
    assert all(write["gateway_id"] == binding["gateway_id"] and write["backend_id"] == binding["backend_id"]
        and write["profile_id"] == binding["profile_id"] for write in browser["writes"])


@pytest.mark.parametrize("runtime_kind", ["web", "node"])
async def test_target_configuration_scopes_use_original_multiroot_writer(
    shared_backend_case: SharedBackendCase, vsix_factory, runtime_kind: str,
) -> None:
    case = shared_backend_case
    primary, secondary, joined = await _join_group(case)
    gateway_id = joined["gateway_id"]
    targets = await case.data("POST", "/api/gateway/extension-targets", json={"protocol_version": 1, "gateway_id": gateway_id})
    target, = targets["targets"]
    originals = [(root / ".boxteam/workspace.jsonc").read_bytes() for root in case.roots]
    package = vsix_factory(case.roots[0], "1.0.0", "configuration-scopes", manifest_overrides={
        "name": "configuration-scopes",
        ("browser" if runtime_kind == "web" else "main"): "./probe.js",
        "extensionKind": ["web" if runtime_kind == "web" else "workspace"],
        "activationEvents": ["onCommand:boxteam.scopes.read"], "contributes": {
            "commands": [{"command": "boxteam.scopes.read", "title": "读取多根目标配置"}],
            "configuration": {"title": "目标作用域验收", "properties": {
                "scopeProbe.value": {"type": "string", "default": "default-value", "scope": "resource"},
                "scopeProbe.language": {"type": "string", "default": "default-language", "scope": "language-overridable"},
                "scopeProbe.inferred": {"type": "string", "default": "default-inferred", "scope": "resource"},
                "scopeProbe.window": {"type": "string", "default": "default-window", "scope": "window"}}},
            "configurationDefaults": {"[typescript]": {"scopeProbe.language": "default-typescript"}}}}, omit=("main",) if runtime_kind == "web" else ())
    with ZipFile(package, "a") as archive:
        archive.writestr("extension/probe.js", """
const vscode = require('vscode');
exports.activate = context => {
  const events = [];
  const activation = Math.random().toString(36);
  const scope = options => {
    const uri = options.uri ? vscode.Uri.parse(options.uri) : options.folder === undefined ? undefined
      : vscode.workspace.workspaceFolders[options.folder].uri;
    return options.language ? { uri, languageId: options.language } : uri;
  };
  const configuration = options => vscode.workspace.getConfiguration('scopeProbe', scope(options));
  const read = (options = {}) => {
    const config = configuration(options);
    return JSON.parse(JSON.stringify({ activation, value: config.get('value'), language: config.get('language'),
      inferred: config.get('inferred'), window: config.get('window'), inspect: config.inspect('value'),
      languageInspect: config.inspect('language'), inferredInspect: config.inspect('inferred'), events,
      folders: vscode.workspace.workspaceFolders.map(folder => folder.uri.toString(true)),
      workspaceFile: vscode.workspace.workspaceFile?.toString(true),
      worker: typeof WorkerGlobalScope !== 'undefined' && self instanceof WorkerGlobalScope,
      pid: typeof process !== 'undefined' && process.versions?.node ? process.pid : null,
    }, (_key, value) => value === undefined ? null : value));
  };
  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration(event => {
    if (event.affectsConfiguration('scopeProbe')) events.push({
      first: read({folder: 0}).value, second: read({folder: 1}).value,
      language: read({folder: 0, language: 'typescript'}).language,
    });
  }), vscode.commands.registerCommand('boxteam.scopes.read', read),
  vscode.commands.registerCommand('boxteam.scopes.update', async options => {
    const target = options.target === undefined ? undefined : vscode.ConfigurationTarget[options.target];
    await configuration(options).update(options.key, options.value, target, options.overrideInLanguage);
    return read(options);
  }));
};
""")
    installed = await case.data("POST", "/api/gateway/extension-installations/install", json={
        "protocol_version": 1, "gateway_id": gateway_id, "backend_id": target["backend_id"], "workspace_ids": [primary],
        "source_uri": resource_display_uri(scope="workspace", scope_id=primary, kind="files",
            tail_segments=("root", package.name), gateway_authority=gateway_id)})
    assert installed["installed"]["id"] == "boxteam-probe.configuration-scopes"
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "目标多根配置验收需要真实 Chromium"
    output = case.context.artifacts_dir / "configuration-scopes.json"
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
        "bun", "tests/integration/clients/web/target_configuration_scopes.mjs"]
    with chromium_temporary_environment(case.context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"]) / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=195, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_BROWSER_BASE_URL": str(case.client.base_url).rstrip("/"), "BOXTEAM_BROWSER_RESULT_PATH": str(output),
                "BOXTEAM_CONFIGURATION_SCOPES": json.dumps({"gateway_id": gateway_id, "backend_id": target["backend_id"],
                    "runtime_kind": runtime_kind, "workspace_ids": [primary, secondary]})})
    (case.context.artifacts_dir / "scopes-browser.stdout.raw").write_text(result.stdout)
    (case.context.artifacts_dir / "scopes-browser.stderr.raw").write_text(result.stderr)
    (case.context.artifacts_dir / "scopes-browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}, indent=2))
    assert result.returncode == 0, result.stdout + result.stderr
    browser = json.loads(output.read_text())
    assert browser["phase"] == "complete" and browser["pageErrors"] == []
    assert browser["binding"]["workspace_ids"] == [primary, secondary]
    workspace = browser["binding"]["context" if runtime_kind == "web" else "initialization"]["workspace"]
    if runtime_kind == "web":
        assert browser["nodeChannels"] == [] and browser["hostRuntime"]["worker"]
        assert browser["hostRuntime"]["pid"] is None
    else:
        assert browser["nodeChannels"] and not browser["hostRuntime"]["worker"]
        assert browser["hostRuntime"]["pid"] > 0
    workspace_uri = urlsplit(workspace["configuration"]["native_uri"])
    assert workspace_uri.scheme == "file" and not workspace_uri.netloc
    workspace_file = Path(url2pathname(workspace_uri.path)).resolve()
    assert workspace_file.is_relative_to(case.boxteam_home.resolve())
    saved = commentjson.loads(workspace_file.read_text())
    assert saved["settings"]["scopeProbe.value"] == "queued-view"
    assert saved["settings"]["scopeProbe.inferred"] == "queued-inferred"
    assert "file has unsaved changes" in browser["dirtyRejection"]
    assert "The content of the file is newer" in browser["saveConflict"]["message"]
    assert browser["saveConflict"]["newerMtime"] > browser["saveConflict"]["originalMtime"]
    assert len(browser["concurrentWrites"]) == 2
    assert len(saved["folders"]) == 2
    first_settings = commentjson.loads((case.roots[0] / ".vscode/settings.json").read_text())
    assert first_settings["scopeProbe.value"] == "inferred-folder"
    assert first_settings["[typescript]"]["scopeProbe.language"] == "folder-typescript"
    assert not (case.roots[1] / ".vscode/settings.json").exists()
    assert all((root / ".boxteam/workspace.jsonc").read_bytes() == original
        for root, original in zip(case.roots, originals, strict=True))
    assert not case.planned_calls
