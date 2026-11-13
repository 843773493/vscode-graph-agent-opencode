"""Skill 来源管理命令沿各自原 owner 打开来源并刷新。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import web_workbench_assets as web_workbench_assets  # noqa: PLC0414
from tests.integration.clients.web.test_workspace_metadata_bootstrap import (
    metadata_case as metadata_case,  # noqa: PLC0414
    metadata_port_reservations as metadata_port_reservations,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(autouse=True)
def skill_management_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_SKILL_MANAGEMENT_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    assert (assets / "version.json").is_file(), assets
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


async def test_skill_management_commands_use_gateway_and_workspace_owners(
    request: pytest.FixtureRequest,
    metadata_case: tuple[object, TestRunContext, Path, object],
) -> None:
    client, context, _workspace, _provider = metadata_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Skill 来源管理验收需要 Chromium"
    gateway_id = (await client.get("/api/gateway/health")).json()["data"]["gateway_id"]
    workspaces = (await client.get("/api/gateway/workspaces?check_health=false")).json()["data"]["items"]
    workspace = next(item for item in workspaces if item["status"] == "ready")
    response = await client.get("/api/gateway/config/sources")
    response.raise_for_status()
    gateway_sources = response.json()["data"]["sources"]
    response = await client.get("/api/v1/config/sources", headers={
        "X-BoxTeam-Target-Gateway-Id": gateway_id,
        "X-BoxTeam-Workspace-Id": workspace["workspace_id"],
    })
    response.raise_for_status()
    workspace_sources = response.json()["data"]["sources"]
    assert gateway_sources and workspace_sources

    result_path = context.artifacts_dir / "skill-management-result.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
        "--timeout-seconds", "180", "--", "node", "tests/integration/clients/web/skill_management.mjs"]
    with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
        completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
            env={**os.environ, **temporary, "BOXTEAM_SKILL_MANAGEMENT_BASE_URL": str(client.base_url).rstrip("/"),
                 "BOXTEAM_SKILL_MANAGEMENT_RESULT": str(result_path), "BOXTEAM_SKILL_MANAGEMENT_GATEWAY_ID": gateway_id,
                 "BOXTEAM_SKILL_MANAGEMENT_WORKSPACE_ID": workspace["workspace_id"],
                 "BOXTEAM_SKILL_MANAGEMENT_WORKSPACE_NAME": workspace["name"],
                 "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
            capture_output=True, text=True, timeout=195, check=False)
    (context.artifacts_dir / "skill-management-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n")
    (context.artifacts_dir / "skill-management.stdout.raw").write_text(completed.stdout, encoding="utf-8")
    (context.artifacts_dir / "skill-management.stderr.raw").write_text(completed.stderr, encoding="utf-8")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    observed = json.loads(result_path.read_text(encoding="utf-8"))
    assert observed["phase"] == "completed"
    assert observed["errors"] == []
    gateway_refresh = next(item for item in observed["requests"] if item["path"] == "/api/gateway/skill-sources/refresh")
    workspace_refresh = next(item for item in observed["requests"] if item["path"] == "/api/v1/resources/skill-sources/refresh")
    assert gateway_refresh["method"] == "POST"
    assert gateway_refresh["headers"].get("x-boxteam-workspace-id") is None
    assert workspace_refresh["method"] == "POST"
    assert workspace_refresh["headers"]["x-boxteam-target-gateway-id"] == gateway_id
    assert workspace_refresh["headers"]["x-boxteam-workspace-id"] == workspace["workspace_id"]
    assert any(item["path"] == "/api/gateway/config/sources" for item in observed["requests"])
    assert any(item["path"].startswith("/api/v1/config/sources/") for item in observed["requests"])
