"""共享来源的两个真实 Workspace 消费者与 Workbench 草稿隔离。"""
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
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture(autouse=True)
def shared_source_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_ENTITIES_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_shared_configuration_keeps_consumer_results_and_separate_drafts(
    mcp_plugin_case: McpPluginCase,
) -> None:
    case = mcp_plugin_case
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "共享来源验收需要真实 Chromium"
    sibling = prepare_default_test_workspace(workspace_root=case.workspace_root / "shared-consumer",
        template_root=case.context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        fresh_identity=True)
    install_test_workspace_config(workspace_root=sibling,
        config_path=case.workspace_root / ".boxteam/workspace.jsonc",
        schema_path=case.context.project_root / "configs/workspace_schema.jsonc")
    assets = Path(os.environ["BOXTEAM_WEB_ASSETS"])
    (case.context.artifacts_dir / "assets.json").write_text(json.dumps({
        "path": str(assets), "main_sha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest(),
    }) + "\n")
    command = ["timeout", "180s", "bash", "-c", 'ulimit -d 4194304; exec "$@"', "bash",
        "bun", "tests/integration/clients/web/configuration_shared_sources.mjs"]
    with chromium_temporary_environment(case.context.runtime_root_for_node("browser") / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=case.context.project_root,
            capture_output=True, text=True, check=False, timeout=195,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                "BOXTEAM_BROWSER_ARTIFACTS": str(case.context.artifacts_dir),
                "BOXTEAM_SHARED_SOURCE_FIXTURE": json.dumps({"baseUrl": str(case.client.base_url),
                    "gatewayId": case.gateway_id, "workspaceId": case.workspace_id,
                    "siblingWorkspace": str(sibling)})})
    (case.context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout)
    (case.context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr)
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }) + "\n")
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{case.context.artifacts_dir}"
    observed = json.loads((case.context.artifacts_dir / "result.json").read_text())
    assert observed["phase"] == "completed" and observed["pageErrors"] == []
    assert observed["modelRequests"] == []
    assert observed["sharedDraftConflict"]["putCountUnchanged"] is True
    assert observed["consumerResults"]["coverage"] == "observed_only"
