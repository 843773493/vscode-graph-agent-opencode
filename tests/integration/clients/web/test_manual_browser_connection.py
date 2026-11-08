from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from tests.harness.python.browser_manager import (
    close_browser_frontend_process,
    start_browser_frontend_process,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式 Workbench 产物
)
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)


@pytest.mark.asyncio
async def test_manual_browser_creation_attaches_and_accepts_first_navigation(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    web_workbench_assets: Path,
) -> None:
    project_root = Path.cwd().resolve()
    workspace_root = Path(integration_workspace_root_path).resolve()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir
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
        pytest.fail("手动浏览器连接 Web Integration 需要 Chromium")

    port_block = integration_port_block_for_file(Path(request.node.fspath))
    gateway_port = port_block.port(20)
    browser_frontend_port = port_block.port(21)
    with ExitStack() as lifetime:
        browser_frontend = start_browser_frontend_process(
            workspace_root=workspace_root,
            artifacts_dir=artifacts,
            frontend_port=browser_frontend_port,
        )
        lifetime.callback(close_browser_frontend_process, browser_frontend)
        gateway = start_gateway_process(
            workspace_root=workspace_root,
            default_backend_url=None,
            port=gateway_port,
            artifacts_dir=artifacts / "gateway",
            boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
            extra_env={
                "BOXTEAM_WEB_ASSETS": str(web_workbench_assets),
                "BOXTEAM_BROWSER_FRONTEND_URL": f"http://127.0.0.1:{browser_frontend_port}",
            },
        )
        lifetime.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway_port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            await acquire_gateway_guest(client)
            workspaces_response = await client.get("/api/gateway/workspaces")
            assert workspaces_response.status_code == 200, workspaces_response.text
            owner = workspaces_response.json()["data"]["active_workspace"]
            workspace_id = owner["workspace_id"]
            gateway_id = owner["gateway_id"]
            client.headers.update({"X-BoxTeam-Workspace-Id": workspace_id,
                                   "X-BoxTeam-Target-Gateway-Id": gateway_id})
            session_title = "手动浏览器首航 E2E"
            session_response = await client.post(
                "/api/v1/sessions",
                json={"title": session_title},
            )
            assert session_response.status_code == 200, session_response.text
            session = session_response.json()["data"]
            assert session["workspace_id"] == workspace_id
            main = await client.get(f"/api/v1/sessions/{session['session_id']}/main-thread")
            assert main.status_code == 200, main.text

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_E2E_BASE_URL": f"http://127.0.0.1:{gateway_port}",
                "BOXTEAM_E2E_FIXTURE": json.dumps(
                    {
                        "sessionTitle": session_title,
                        "workspaceId": workspace_id,
                        "sessionId": session["session_id"],
                        "gatewayId": gateway_id,
                        "threadId": main.json()["data"]["thread_id"],
                    },
                    ensure_ascii=False,
                ),
                "BOXTEAM_E2E_RESULT_PATH": str(result_path),
                "BOXTEAM_E2E_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        temporary = context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp"
        with chromium_temporary_environment(temporary) as temporary_environment:
            playwright_result = await asyncio.to_thread(
                subprocess.run,
                ["bun", "tests/integration/clients/web/manual_browser_connection.mjs"],
                cwd=project_root, env={**environment, **temporary_environment},
                capture_output=True, text=True, timeout=120, check=False,
            )
        (artifacts / "browser.stdout.raw").write_text(playwright_result.stdout)
        (artifacts / "browser.stderr.raw").write_text(playwright_result.stderr)
        (artifacts / "browser-process.json").write_text(json.dumps({"exit_code": playwright_result.returncode}) + "\n")
        assert playwright_result.returncode == 0, (
            "手动浏览器连接 Web Integration 失败:\n"
            f"stdout:\n{playwright_result.stdout}\n"
            f"stderr:\n{playwright_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        browser_result = json.loads(result_path.read_text(encoding="utf-8"))
        assert browser_result["preview_owner"] == {
            "workspaceId": session["workspace_id"], "sessionId": session["session_id"],
            "threadId": main.json()["data"]["thread_id"],
        }
        assert browser_result["popup_count"] == 0
        assert browser_result["creation_owner"] == {
            "gatewayId": gateway_id, "workspaceId": workspace_id, "sessionId": session["session_id"],
            "threadId": main.json()["data"]["thread_id"],
        }
        assert browser_result["transition"]["badge"] in {"正在初始化", "连接中"}
        assert browser_result["final"]["badge"].startswith("已连接")
        assert "新建浏览器成功" in browser_result["final"]["parentNotice"]
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
