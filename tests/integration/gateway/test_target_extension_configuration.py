"""正式 Workbench／同字节 VSIX／双目标与同目标双窗口的用户配置集成验收。"""

from __future__ import annotations

import asyncio
import base64
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

from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    InstallationCase,
    installation_route,
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    installation_case as installation_case,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_dynamic_provider import (
    secondary_case as secondary_case,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment

EXTENSION_ID = "boxteam-probe.target-configuration"
SECTION = "targetConfigurationProbe"


def _configuration_vsix(workspace: Path) -> Path:
    """只准备标准 VSIX 输入；配置注册与实际写入均沿产品入口。"""
    target = workspace / "target-configuration.vsix"
    manifest = {
        "name": "target-configuration", "publisher": "boxteam-probe", "version": "1.0.0", "type": "module",
        "engines": {"vscode": "^1.0.0"}, "main": "./extension.mjs", "extensionKind": ["workspace"],
        "activationEvents": ["onStartupFinished"], "contributes": {
            "commands": [{"command": f"target-configuration-probe.{name}", "title": f"目标配置 {name}"}
                for name in ("read", "update", "update-block")],
            "configuration": {"title": "目标配置探针", "properties": {
                f"{SECTION}.value": {"type": "string", "default": "value-default", "scope": "window"},
                f"{SECTION}.application": {"type": "string", "default": "application-default", "scope": "application"},
                f"{SECTION}.language": {"type": "string", "default": "language-default", "scope": "language-overridable"},
            }},
        },
    }
    code = """
import * as vscode from 'vscode';
const section = 'targetConfigurationProbe';
const inspectFields = ['defaultValue', 'globalValue', 'globalLocalValue', 'globalRemoteValue',
  'globalLanguageValue', 'globalLocalLanguageValue', 'globalRemoteLanguageValue',
  'workspaceValue', 'workspaceFolderValue', 'workspaceLanguageValue', 'workspaceFolderLanguageValue'];
export function activate(context) {
  const resource = vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'configuration-probe.ts');
  const events = [];
  const inspect = (configuration, key) => {
    const value = configuration.inspect(key);
    if (!value) throw new Error('配置未注册：' + key);
    return Object.fromEntries(inspectFields.map(field => [field, value[field] ?? null]));
  };
  const read = () => {
    const base = vscode.workspace.getConfiguration(section);
    const language = vscode.workspace.getConfiguration(section, { uri: resource, languageId: 'typescript' });
    return { extensionId: context.extension.id,
      base: { value: base.get('value'), application: base.get('application'), language: base.get('language') },
      typescript: language.get('language'),
      inspect: Object.fromEntries(['value', 'application', 'language'].map(key => [key, inspect(base, key)])),
      typescriptInspect: inspect(language, 'language'), events: [...events] };
  };
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration(event => {
      if (event.affectsConfiguration(section)) events.push({
        value: event.affectsConfiguration(section + '.value'),
        application: event.affectsConfiguration(section + '.application'),
        language: event.affectsConfiguration(section + '.language', { uri: resource, languageId: 'typescript' }),
      });
    }),
    vscode.commands.registerCommand('target-configuration-probe.read', read),
    vscode.commands.registerCommand('target-configuration-probe.update', async change => {
      const scope = change.languageId ? { uri: resource, languageId: change.languageId } : undefined;
      await vscode.workspace.getConfiguration(section, scope).update(change.key, change.value,
        vscode.ConfigurationTarget.Global, change.scopeToLanguage);
      return read();
    }),
    vscode.commands.registerCommand('target-configuration-probe.update-block', async block => {
      await vscode.workspace.getConfiguration().update('[typescript]', block, vscode.ConfigurationTarget.Global);
      return read();
    }));
}
"""
    with ZipFile(target, "w") as archive:
        archive.writestr("extension/package.json", json.dumps(manifest))
        archive.writestr("extension/extension.mjs", code)
    return target


def _install(case: InstallationCase, vsix: Path, label: str) -> dict:
    target, _ = installation_route(case.client)
    response = case.client.get("/api/gateway/workspaces")
    response.raise_for_status()
    listing = response.json()
    assert listing["request_id"] == response.headers["X-Request-ID"]
    owner = listing["data"]["active_workspace"]
    response = case.client.post("/api/gateway/extension-installations/install", json={
        "protocol_version": 1, "gateway_id": target["gateway_id"], "backend_id": target["backend_id"],
        "workspace_ids": [owner["workspace_id"]],
        "source_uri": f"boxteam://workspace/{owner['workspace_id']}/resources/files/root/{vsix.name}",
    })
    assert response.status_code == 200, response.text
    installed = response.json()
    assert installed["request_id"] == response.headers["X-Request-ID"]
    assert installed["data"]["installed"]["id"] == EXTENSION_ID
    (case.context.artifacts_dir / f"configuration-install-{label}.json").write_text(
        json.dumps({"target": target, "owner": owner, "installed": installed,
            "vsix_sha256": hashlib.sha256(vsix.read_bytes()).hexdigest()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"gateway_id": target["gateway_id"], "backend_id": target["backend_id"], "workspace_id": owner["workspace_id"]}


async def test_global_settings_follow_target_and_notify_another_window(
    installation_case: InstallationCase, secondary_case: InstallationCase,
) -> None:
    case = installation_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "目标配置验收需要真实 Chromium"
    manifest_value = os.getenv("BOXTEAM_TEST_MCP_RUNTIME_MANIFEST")
    assert manifest_value, "目标配置验收需要匹配当前源码的原 Node component"
    manifest = Path(manifest_value)
    original_workspace = [(current.workspace / ".boxteam/workspace.jsonc").read_bytes()
        for current in (case, secondary_case)]
    vsix = _configuration_vsix(case.workspace)
    secondary_vsix = secondary_case.workspace / vsix.name
    shutil.copyfile(vsix, secondary_vsix)
    assert secondary_vsix.read_bytes() == vsix.read_bytes(), "A/B 必须安装同字节 VSIX"
    owners = {"a": _install(case, vsix, "a"), "b": _install(secondary_case, secondary_vsix, "b")}
    assert owners["a"]["gateway_id"] != owners["b"]["gateway_id"]
    assert owners["a"]["backend_id"] != owners["b"]["backend_id"]
    paths = ["src/clients/workbench/scripts/extensions/extensionConfigurationTransform.mjs",
        "src/clients/workbench/src/extensions/configuration.ts", "src/clients/workbench/src/extensions/configurationFiles.ts",
        "tests/integration/gateway/test_target_extension_configuration.py",
        "tests/integration/clients/web/target_extension_configuration.mjs"]
    (case.context.artifacts_dir / "configuration-inputs.json").write_text(json.dumps({
        "sha256": {path: hashlib.sha256((Path.cwd() / path).read_bytes()).hexdigest() for path in paths},
        "runtime_manifest": str(manifest), "runtime_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }, indent=2) + "\n", encoding="utf-8")
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
        "bun", "tests/integration/clients/web/target_extension_configuration.mjs"]
    runtime = case.context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"])
    with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, timeout=195, capture_output=True, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_TARGET_EXTENSION_CONFIGURATION": json.dumps({"baseUrl": str(case.client.base_url).rstrip("/"),
                    "secondaryBaseUrl": str(secondary_case.client.base_url).rstrip("/"), "owners": owners,
                    "artifacts": str(case.context.artifacts_dir)})})
    (case.context.artifacts_dir / "browser.stdout.raw").write_bytes(result.stdout)
    (case.context.artifacts_dir / "browser.stderr.raw").write_bytes(result.stderr)
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
        "exit_code": result.returncode, "memory_limit_kb": 4194304, "timeout_seconds": 180}, indent=2) + "\n", encoding="utf-8")
    assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="backslashreplace")
    browser = json.loads((case.context.artifacts_dir / "target-configuration-browser.json").read_text(encoding="utf-8"))
    assert browser["phase"] == "complete"
    expected = {
        "a": {f"{SECTION}.value": "A-after-B-focus", f"{SECTION}.application": "A-second-window",
            f"{SECTION}.language": "A-language-base", "[typescript]": {f"{SECTION}.language": "A-typescript-implicit"}},
        "b": {f"{SECTION}.value": "B-global", f"{SECTION}.application": "B-application",
            f"{SECTION}.language": "B-language-base", "[typescript]": {f"{SECTION}.language": "B-typescript"}},
    }
    native = {}
    for label, current, original in zip(("a", "b"), (case, secondary_case), original_workspace, strict=True):
        record = browser["targets"][label]
        resource = record["binding"]["initialization"]["environment"]["remote_settings"]
        uri = urlsplit(resource["native_uri"])
        assert uri.scheme == "file" and not uri.netloc and not uri.query and not uri.fragment
        settings = Path(url2pathname(uri.path)).resolve()
        assert settings.is_relative_to(current.home.resolve()), settings
        saved = settings.read_bytes()
        assert saved == base64.b64decode(record["finalSettings"]["content_base64"], validate=True)
        assert commentjson.loads(saved.decode("utf-8")) == expected[label]
        assert (current.workspace / ".boxteam/workspace.jsonc").read_bytes() == original
        assert not (current.workspace / ".vscode/settings.json").exists(), "Global 写入不能落到工作区"
        writes = record["writes"] + (browser["targets"]["a2"]["writes"] if label == "a" else [])
        assert all(body["gateway_id"] == owners[label]["gateway_id"] and body["backend_id"] == owners[label]["backend_id"]
            and body["resource_uri"] == resource["resource_uri"] for body in writes)
        assert writes, "必须观察到原配置 FileService 的真实写入"
        native[label] = {"path": str(settings), "sha256": hashlib.sha256(saved).hexdigest(),
            "content_base64": base64.b64encode(saved).decode(), "settings": expected[label]}
    assert native["a"]["path"] != native["b"]["path"]
    assert browser["targets"]["b"]["beforeForeignWrites"] == browser["targets"]["b"]["finalSettings"]
    (case.context.artifacts_dir / "configuration-native-bytes.json").write_text(
        json.dumps(native, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
