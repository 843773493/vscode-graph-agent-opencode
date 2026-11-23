"""Gateway 遥测设置、实际回执和安全诊断复制经正式 Workbench 验收。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from boxteam_plugin_sdk.logging_contract import load_runtime_log_contract

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(autouse=True)
def observation_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_GATEWAY_MANAGEMENT_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_gateway_runtime_settings_and_diagnostic_summary(
    request: pytest.FixtureRequest,
    metadata_case: tuple[object, TestRunContext, Path, object],
) -> None:
    client, context, workspace, _provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Gateway 诊断验收需要真实 Chromium"
    gateway_id = (await client.get("/api/gateway/health")).json()["data"]["gateway_id"]
    workspace_id = (await client.get("/api/v1/workspace")).json()["data"]["workspace_id"]
    assets = Path(os.environ["BOXTEAM_WEB_ASSETS"])
    product = json.loads((assets / "version.json").read_text())["product"]
    home = context.boxteam_home_for_node(request.node.nodeid)
    (context.artifacts_dir / "assets.json").write_text(json.dumps({
        "path": str(assets), "main_sha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest(),
    }) + "\n")
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
        "bun", "tests/integration/clients/web/gateway_runtime_observation.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            capture_output=True, text=True, timeout=195, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_BROWSER_ARTIFACTS": str(context.artifacts_dir), "BOXTEAM_GATEWAY_OBSERVATION_FIXTURE": json.dumps({
                    "baseUrl": str(client.base_url).rstrip("/"), "gatewayId": gateway_id, "workspaceId": workspace_id,
                    "workspaceData": str(workspace / ".boxteam"), "gatewayControl": str(home / "state/gateway"),
                    "gatewayLogs": str(load_runtime_log_contract().root_for(home)),
                    "product": product})})
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": completed.returncode}) + "\n")
    (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads((context.artifacts_dir / "result.json").read_text())
    assert result["phase"] == "completed" and result["pageErrors"] == []
    assert result["unexpectedCalls"] == []
    assert result["summarySafe"] and result["appliedSettings"]
    assert result["storageLocations"] and result["retentionOwner"] == gateway_id
