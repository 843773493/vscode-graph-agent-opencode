"""真实 Web/Gateway/Backend 完整组交互，仅 Provider 使用协议桩。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.gateway.workspace_ids import build_managed_local_workspace_id
from tests.integration.test_shared_managed_backend_lifecycle import SharedBackendCase

pytest_plugins = ("tests.integration.test_shared_managed_backend_lifecycle",)


@pytest.fixture(autouse=True)
def managed_web_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path.cwd()
    build = subprocess.run(
        ["bun", "run", "--cwd", "src/clients/web", "build"],
        cwd=root, capture_output=True, text=True, timeout=120, check=False,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(root / "src/clients/web/dist"))


@pytest.mark.asyncio
async def test_complete_backend_group_confirmation_and_runtime_actions(
    shared_backend_case: SharedBackendCase,
) -> None:
    case = shared_backend_case
    initial = await case.data("GET", "/api/gateway/workspaces")
    primary = next(item for item in initial["items"] if item["system_default"])
    added = await case.data("POST", "/api/gateway/workspaces/local", json={
        "root_path": str(case.roots[1]), "name": "Web Secondary",
    })
    secondary = next(item for item in added["items"] if item["workspace_id"] == build_managed_local_workspace_id(str(case.roots[1])))
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
    process = await asyncio.to_thread(
        subprocess.run,
        ["bun", "tests/integration/clients/web/managed_backend_runtime.mjs"],
        cwd=Path.cwd(), env=environment, capture_output=True, text=True, timeout=420, check=False,
    )
    (case.context.artifacts_dir / "playwright.log").write_text(process.stdout + process.stderr)
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
