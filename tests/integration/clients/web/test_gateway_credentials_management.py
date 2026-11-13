"""设备与凭据入口经完整 Workbench 和真实 Gateway 授权 owner 验收。"""
from __future__ import annotations

import asyncio
import hashlib
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
def gateway_credentials_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_GATEWAY_MANAGEMENT_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_gateway_credentials_use_existing_device_owner(mcp_plugin_case: McpPluginCase) -> None:
    case = mcp_plugin_case
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "设备与凭据入口验收需要 Chromium"
    assets = Path(os.environ["BOXTEAM_WEB_ASSETS"])
    (case.context.artifacts_dir / "assets.json").write_text(json.dumps({
        "path": str(assets), "main_sha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest(),
    }) + "\n")
    command = ["timeout", "100s", "bash", "-c", 'ulimit -d 4194304; exec "$@"', "bash",
        "bun", "tests/integration/clients/web/gateway_credentials_management.mjs"]
    with chromium_temporary_environment(case.context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, cwd=case.context.project_root,
            capture_output=True, text=True, check=False, timeout=110,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                "BOXTEAM_BROWSER_ARTIFACTS": str(case.context.artifacts_dir),
                "BOXTEAM_GATEWAY_MANAGEMENT_FIXTURE": json.dumps({"baseUrl": str(case.client.base_url),
                    "gatewayId": case.gateway_id, "workspaceId": case.workspace_id})})
    (case.context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout)
    (case.context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr)
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
        "exit_code": result.returncode}) + "\n")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}\n{case.context.artifacts_dir}"
    observed = json.loads((case.context.artifacts_dir / "result.json").read_text())
    assert observed["phase"] == "completed"
    assert observed["pageErrors"] == []
    assert observed["unexpectedCalls"] == []
    assert observed["writes"] == {"create": 2, "revoke": 3}
    assert observed["secretAbsentFromStorage"] is True
