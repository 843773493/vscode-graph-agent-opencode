from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

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
async def test_web_bootstrap_and_resource_refresh_recover_gateway_auth(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    web_workbench_assets: Path,
) -> None:
    project_root = Path.cwd().resolve()
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("Gateway 认证恢复 Web Integration 需要 Chromium")

    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir

    port_block = integration_port_block_for_file(Path(request.node.fspath))
    gateway = start_gateway_process(
        workspace_root=Path(integration_workspace_root_path),
        default_backend_url=None,
        port=port_block.port(20),
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=artifacts / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)},
    )
    result_path = artifacts / "gateway-auth-resource-recovery-result.json"
    screenshot_path = artifacts / "gateway-auth-resource-recovery-failure.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    try:
        # Gateway 托管完整工作区运行时，资源查询必须连接真实 Terminal/Browser owner。
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}",
                                     headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            await acquire_gateway_guest(client)
            session_response = await client.post("/api/v1/sessions", json={"title": "Gateway 认证恢复资源面板"})
            assert session_response.status_code == 200, session_response.text
            session = session_response.json()["data"]
            main = await client.get(f"/api/v1/sessions/{session['session_id']}/main-thread")
            assert main.status_code == 200, main.text
        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                "BOXTEAM_BROWSER_OWNER": json.dumps({"sessionId": session["session_id"],
                    "workspaceId": session["workspace_id"], "threadId": main.json()["data"]["thread_id"]}),
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        temporary = context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp"
        with chromium_temporary_environment(temporary) as temporary_environment:
            browser_result = await asyncio.to_thread(
                subprocess.run,
                ["bun", "tests/integration/clients/web/gateway_auth_resource_recovery.mjs"],
                cwd=project_root, env={**environment, **temporary_environment},
                capture_output=True, text=True, timeout=120, check=False,
            )
        (artifacts / "browser.stdout.raw").write_text(browser_result.stdout)
        (artifacts / "browser.stderr.raw").write_text(browser_result.stderr)
        (artifacts / "browser-process.json").write_text(json.dumps({"exit_code": browser_result.returncode}) + "\n")
        assert browser_result.returncode == 0, (
            "Gateway 认证恢复 Web Integration 失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["bootstrapWithoutCookie"] is True
        assert result["resourceUnauthorizedThenRecovered"] is True
        assert result["resourcePanelErrorAbsent"] is True
        assert result["resourcePanelVisible"] is True
        assert result["resourcePanelFillsBody"] is True
        assert result["debugPanelFillsBody"] is True
        assert result["hiddenPollingStopped"] is True
        assert result["visiblePollingRestored"] is True
        diagnostic = result["navigationDiagnostic"]
        assert diagnostic["draftRetained"] is True
        assert diagnostic["recoveredRequestId"]
        assert diagnostic["requests"]
        assert diagnostic["diagnosticReads"] == []
        assert "完整错误末尾" in diagnostic["detail"]
        assert '<script>throw new Error("不得执行诊断正文")</script>' in diagnostic["detail"]
        assert result["noPageErrors"] is True
    finally:
        close_gateway_process(gateway)
