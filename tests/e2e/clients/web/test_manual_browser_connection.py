from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.e2e.gateway.browser_manager import (
    close_browser_frontend_process,
    start_browser_frontend_process,
)
from tests.e2e.gateway.processes import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import e2e_port_block_for_file


@pytest.mark.asyncio
async def test_manual_browser_creation_attaches_and_accepts_first_navigation(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
) -> None:
    project_root = Path.cwd().resolve()
    workspace_root = Path(e2e_workspace_root_path).resolve()
    artifacts = workspace_root.parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "manual-browser-connection-result.json"
    screenshot_path = artifacts / "manual-browser-connection-failure.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)

    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("手动浏览器连接 Web E2E 需要 Chromium")

    build = subprocess.run(
        ["bun", "run", "build"],
        cwd=project_root / "src" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    port_block = e2e_port_block_for_file(Path(request.node.fspath))
    gateway_port = port_block.port(20)
    browser_frontend_port = port_block.port(21)
    browser_frontend = start_browser_frontend_process(
        workspace_root=workspace_root,
        frontend_port=browser_frontend_port,
    )
    gateway = None
    try:
        gateway = start_gateway_process(
            workspace_root=workspace_root,
            default_backend_url="managed-by-gateway",
            port=gateway_port,
            extra_env={
                "BOXTEAM_WEB_ASSETS": str(project_root / "src" / "web" / "dist"),
                "BOXTEAM_BROWSER_FRONTEND_URL": f"http://127.0.0.1:{browser_frontend_port}",
            },
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway_port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            workspaces_response = await client.get("/api/gateway/workspaces")
            assert workspaces_response.status_code == 200, workspaces_response.text
            workspace_id = workspaces_response.json()["data"]["active_workspace_id"]
            session_title = "手动浏览器首航 E2E"
            session_response = await client.post(
                "/api/v1/sessions",
                json={"title": session_title},
            )
            assert session_response.status_code == 200, session_response.text

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_E2E_BASE_URL": f"http://127.0.0.1:{gateway_port}",
                "BOXTEAM_E2E_FIXTURE": json.dumps(
                    {
                        "sessionTitle": session_title,
                        "workspaceId": workspace_id,
                    },
                    ensure_ascii=False,
                ),
                "BOXTEAM_E2E_RESULT_PATH": str(result_path),
                "BOXTEAM_E2E_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        playwright_result = subprocess.run(
            ["node", "tests/e2e/clients/web/manual_browser_connection.mjs"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert playwright_result.returncode == 0, (
            "手动浏览器连接 Web E2E 失败:\n"
            f"stdout:\n{playwright_result.stdout}\n"
            f"stderr:\n{playwright_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        browser_result = json.loads(result_path.read_text(encoding="utf-8"))
        assert browser_result["transition"]["badge"] in {"正在初始化", "连接中"}
        assert browser_result["final"]["badge"].startswith("已连接")
        assert "已在预览区打开" in browser_result["final"]["parentNotice"]
        assert "正在" not in browser_result["final"]["parentNotice"]
        assert browser_result["final"]["focusedElement"] == "address-input"
        assert browser_result["final"]["submittedDuringInitialization"] is True
        assert browser_result["final"]["firstNavigationTitle"] == "Manual Ready"
        assert browser_result["final"]["delayedInitialSnapshot"] is True
        assert browser_result["final"]["delayedClientModule"] is True
        assert browser_result["final"]["clientModuleCacheControl"] == "no-store"
        assert browser_result["random_uuid_unavailable"]["randomUuidType"] == "undefined"
        assert browser_result["random_uuid_unavailable"]["ready"] is True
        assert browser_result["random_uuid_unavailable"]["badge"].startswith("已连接")
        assert "点击画面区域重新加载" in browser_result["recovery"]["failedStatus"]
        assert browser_result["recovery"]["reloadedBadge"].startswith("已连接")
        assert "ERR_" in browser_result["tab_and_navigation_failure"]["failedStatus"]
        assert "ERR_" in browser_result["tab_and_navigation_failure"]["failedOverlay"]
        assert "null/" not in browser_result["tab_and_navigation_failure"]["failedStatus"]
        assert "\x1b" not in browser_result["tab_and_navigation_failure"]["failedStatus"]
        assert "\x1b" not in browser_result["tab_and_navigation_failure"]["failedOverlay"]
        assert browser_result["tab_and_navigation_failure"]["recoveredTitle"] == "RECOVERED_BLUE"
        assert "ERR_" not in browser_result["tab_and_navigation_failure"]["recoveredStatus"]
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        close_browser_frontend_process(browser_frontend)
