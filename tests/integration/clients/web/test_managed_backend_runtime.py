"""真实 Web/Gateway/Backend 完整组交互，仅 Provider 使用协议桩。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.workspace_identity import read_workspace_id
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式 Workbench 产物
)
from tests.integration.test_shared_managed_backend_lifecycle import SharedBackendCase
from tests.integration.test_shared_managed_backend_lifecycle import (
    shared_backend_case as shared_backend_case,  # noqa: PLC0414 - 复用真实 Gateway／Backend 装具
)
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(autouse=True)
def managed_web_assets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = os.getenv("BOXTEAM_RUNTIME_MANAGEMENT_WEB_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(assets))


@pytest.mark.asyncio
async def test_complete_backend_group_confirmation_and_runtime_actions(
    shared_backend_case: SharedBackendCase, request: pytest.FixtureRequest,
) -> None:
    case = shared_backend_case
    initial = await case.data("GET", "/api/gateway/workspaces")
    primary = next(item for item in initial["items"] if item["system_default"])
    assert primary["workspace_id"] == read_workspace_id(case.roots[0])
    added = await case.data("POST", "/api/gateway/workspaces/local", json={
        "root_path": str(case.roots[1]), "name": "Web Secondary",
    })
    secondary_id = read_workspace_id(case.roots[1])
    secondary = next(item for item in added["items"]
                     if item["gateway_id"] == primary["gateway_id"] and item["workspace_id"] == secondary_id)
    await case.data("POST", f"/api/gateway/workspaces/{secondary['workspace_id']}/runtime/stop")
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    assert chromium is not None, "Backend 完整组 Web Integration 需要 Chromium"
    result_path = case.context.artifacts_dir / "managed-backend-web-result.json"
    screenshot_path = case.context.artifacts_dir / "managed-backend-web.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    assets = Path(os.environ["BOXTEAM_WEB_ASSETS"])
    (case.context.artifacts_dir / "assets.json").write_text(json.dumps({
        "path": str(assets), "main_sha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest(),
    }) + "\n")
    environment = {
        **os.environ,
        "BOXTEAM_BROWSER_BASE_URL": str(case.client.base_url).rstrip("/"),
        "BOXTEAM_BROWSER_FIXTURE": json.dumps({
            "gatewayId": primary["gateway_id"],
            "primaryId": primary["workspace_id"], "primaryName": primary["name"],
            "secondaryId": secondary["workspace_id"], "secondaryName": secondary["name"],
        }, ensure_ascii=False),
        "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
        "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    temporary = case.context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp"
    supervisor = case.context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "420",
        "--", "bun", "tests/integration/clients/web/managed_backend_runtime.mjs"]
    with chromium_temporary_environment(temporary) as temporary_environment:
        process = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=Path.cwd(), env={**environment, **temporary_environment},
            capture_output=True, text=True, timeout=435, check=False,
        )
    (case.context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout)
    (case.context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr)
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": process.returncode}) + "\n")
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(result_path.read_text())
    assert result["cancel"]["lifecycle_posts"] == 0
    assert result["blocked"]["status"] == "blocked"
    assert result["forced"]["forced"] is True
    assert result["old_stream"]["closed"] is True
    assert result["removed"]["status"] == "restarted"
    assert result["empty_exit"]["status"] == "stopped"
    assert result["deleted"]["request"]["desired_workspace_ids"] == []
    assert not any(item["workspace_id"] == secondary["workspace_id"] for item in result["deleted"]["workspaces"]["items"])
    assert result["route_stubs"] == []
    assert result["errors"] == []
    assert result["first_available_ms"] <= 3000
    assert result["warm_api_budget"]["p95"] <= 500
    assert result["warm_api_budget"]["max"] <= 1500
    # 公开错误正文不复制诊断原文，按请求 ID 对齐真实服务异常链，避免误认其它 409。
    diagnostic = Path(case.gateway.stderr_file.name).read_text()
    entries = re.split(r"(?m)(?=^\d{4}-\d{2}-\d{2}T)", diagnostic)
    stale = next(entry for entry in entries
                 if f'request_id="{result["stale"]["request_id"]}"' in entry
                 and "logger=app.core.failures" in entry)
    assert "ValueError: managed Backend generation 已陈旧；重新获取完整组状态" in stale
