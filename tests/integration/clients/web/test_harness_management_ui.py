"""正式 Workbench 的管理侧栏、原生菜单和固定 owner 详情。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import McpPluginCase
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_case as mcp_plugin_case,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_plugin_lifecycle import (
    mcp_plugin_tool_restrictions as mcp_plugin_tool_restrictions,  # noqa: PLC0414
)
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.paths import chromium_temporary_environment
from tests.support.plugin_package import install_plugin_async
from tests.support.ports import integration_port_block_for_file


@pytest.fixture(autouse=True)
def management_ui_environment(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    prepared = os.getenv("BOXTEAM_MANAGEMENT_UI_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))
    (context.artifacts_dir / "assets.json").write_text(json.dumps({
        "path": str(assets), "main_sha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest(),
    }) + "\n")
    ports = integration_port_block_for_file(Path(request.node.path))
    lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "ports.json", (ports.port(20), ports.port(21)))
    lease.acquire()
    try:
        yield
    finally:
        lease.release()


async def test_management_sidebar_and_native_editors(mcp_plugin_case: McpPluginCase) -> None:
    case = mcp_plugin_case
    manifest = {"manifest_version": 1, "package_id": "proof.management", "version": "1.0.0",
        "name": "管理界面测试包", "description": "同一包的独立插件和固定管理范围", "extension_contract_version": 1,
        "plugins": [{"id": plugin_id, "name": name, "instance_scope": scope,
            "requires": [{"extension_point": "configuration", "version": 1}],
            "contributes": {"configuration": {"type": "object", "properties": {"label": {"type": "string"}}}}}
            for plugin_id, name, scope in [("workspace-probe", "工作区探针", "workspace"), ("gateway-probe", "Gateway 探针", "gateway")]]}
    archive = case.context.artifacts_dir / "management.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("plugin.jsonc", json.dumps(manifest, ensure_ascii=False))
    await install_plugin_async(lambda path, body: case.client.post(path, json=body), gateway_id=case.gateway_id,
        path=str(archive), expected_digest=hashlib.sha256(archive.read_bytes()).hexdigest())
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "管理界面验收需要 Chromium"
    command = ["bun", "tests/integration/clients/web/harness_management_ui.mjs"]
    with chromium_temporary_environment(case.context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, cwd=case.context.project_root,
            capture_output=True, text=True, check=False, timeout=120,
            env={**os.environ, **temporary, **case.replay.environment, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                 "BOXTEAM_BROWSER_ARTIFACTS": str(case.context.artifacts_dir), "BOXTEAM_BROWSER_FIXTURE": json.dumps({
                     "baseUrl": str(case.client.base_url), "gatewayId": case.gateway_id, "workspaceId": case.workspace_id})})
    (case.context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout)
    (case.context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr)
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}) + "\n")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}\n{case.context.artifacts_dir}"
    observed = json.loads((case.context.artifacts_dir / "result.json").read_text())
    assert observed["phase"] == "completed"
    assert observed["pageErrors"] == []
    assert observed["approvalRequests"] == []
    assert [write["workspace_id"] for write in observed["writes"]] == [case.workspace_id, case.workspace_id]
    assert observed["welcomeManagement"] == {
        "entries": ["boxteam.manageProviders", "boxteam.manageAgents", "boxteam.manageGateway"],
        "noSession": True,
        "activeWorkspacePreserved": True,
        "owner": {"gatewayId": case.gateway_id, "workspaceId": case.workspace_id},
    }
    assert observed["providerManagementInSixThreads"] == {
        "loaded": True, "closed": True, "threadCountPreserved": 6,
        "visibleThreadPanesDuringManagement": 5,
        "defaultGatewayId": case.gateway_id, "workspaceId": case.workspace_id,
        "activeWorkspacePreserved": True, "noActivation": True,
    }
    assert observed["bilingualProviderManagement"] == {"chinese": True, "english": True}
    assert observed["appearanceManagement"] == {
        "command": "boxteam.appearance.open", "menuEntry": "BoxTeam 外观", "categoryVisible": True,
    }
    assert observed["appearanceManagementEnglish"] is True
    assert observed["keyboardShortcuts"] == {"loaded": True}
    assert observed["profiles"] == {"loaded": True}
    assert observed["multiThreadManagement"] == {
        "threadCount": 6, "editorGroups": 6, "managementOpenedAndClosed": True,
        "composerContinued": True, "unsentDraftPreserved": True,
        "modelStreamResponses": 2,
        "cacheInformationRead": True, "cacheOwnerStableAfterFocusChange": True,
    }
    assert len(observed["messagePosts"]) == 2
    assert observed["activationRequests"] == []
    assert len(observed["sessionCreates"]) == 6
