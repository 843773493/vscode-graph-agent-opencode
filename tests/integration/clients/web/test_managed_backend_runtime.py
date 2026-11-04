"""真实 Web/Gateway/Backend 完整组交互，仅 Provider 使用协议桩。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.workspace_identity import read_workspace_id
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式 Workbench 产物
)
from tests.integration.test_shared_managed_backend_lifecycle import SharedBackendCase
from tests.support.paths import chromium_temporary_environment

pytest_plugins = ("tests.integration.test_shared_managed_backend_lifecycle",)


@pytest.fixture(autouse=True)
def managed_web_assets(monkeypatch: pytest.MonkeyPatch, web_workbench_assets: Path) -> None:
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))


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
    environment = {
        **os.environ,
        "BOXTEAM_BROWSER_BASE_URL": str(case.client.base_url).rstrip("/"),
        "BOXTEAM_BROWSER_FIXTURE": json.dumps({
            "primaryId": primary["workspace_id"], "primaryName": primary["name"],
            "secondaryId": secondary["workspace_id"], "secondaryName": secondary["name"],
        }, ensure_ascii=False),
        "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
        "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
    }
    temporary = case.context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp"
    with chromium_temporary_environment(temporary) as temporary_environment:
        process = await asyncio.to_thread(
            subprocess.run,
            ["bun", "tests/integration/clients/web/managed_backend_runtime.mjs"],
            cwd=Path.cwd(), env={**environment, **temporary_environment},
            capture_output=True, text=True, timeout=420, check=False,
        )
    (case.context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout)
    (case.context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr)
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"exit_code": process.returncode}) + "\n")
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
    # 公开错误正文不复制诊断原文，按请求 ID 对齐真实服务异常链，避免误认其它 409。
    diagnostic = Path(case.gateway.stderr_file.name).read_text()
    entries = re.split(r"(?m)(?=^\d{4}-\d{2}-\d{2}T)", diagnostic)
    stale = next(entry for entry in entries
                 if f'request_id="{result["stale"]["request_id"]}"' in entry
                 and "logger=app.core.failures" in entry)
    assert "ValueError: managed Backend generation 已陈旧；重新获取完整组状态" in stale
