"""无 Session 的 Workspace + Agent 工具管理经真实 Workbench 与 Gateway 验收。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

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
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(autouse=True)
def tool_management_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_TOOLS_UI_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "out/main.js").is_file() and (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_workspace_tools_without_session_use_exact_workspace_agent_owner(mcp_plugin_case: McpPluginCase) -> None:
    case = mcp_plugin_case
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "工具管理 Gateway 验收需要 Chromium"
    artifacts = case.context.artifacts_dir / "browser"
    artifacts.mkdir()
    command = ["bun", "tests/integration/clients/web/workspace_tool_management.mjs"]
    with chromium_temporary_environment(case.context.runtime_root_for_node("workspace-tools-browser") / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, cwd=case.context.project_root,
            capture_output=True, text=True, check=False, timeout=150,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                 "BOXTEAM_BROWSER_ARTIFACTS": str(artifacts), "BOXTEAM_TOOLS_FIXTURE": json.dumps({
                     "baseUrl": str(case.client.base_url), "gatewayId": case.gateway_id, "workspaceId": case.workspace_id})})
    (artifacts / "browser.stdout.raw").write_text(result.stdout)
    (artifacts / "browser.stderr.raw").write_text(result.stderr)
    (artifacts / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}) + "\n")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}\n{artifacts}"
    observed = json.loads((artifacts / "result.json").read_text())
    assert observed["phase"] == "completed"
    assert observed["pageErrors"] == []
    assert observed["changes"]
    assert not any(request["method"] == "POST" and "/tests" in request["url"] for request in observed["requests"])
