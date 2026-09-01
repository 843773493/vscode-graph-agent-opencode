from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.support.gateway_processes import (
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file


@pytest.mark.asyncio
async def test_web_bootstrap_and_resource_refresh_recover_gateway_auth(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_backend_port: int,
    integration_client: httpx.AsyncClient,
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

    session_title = "Gateway 认证恢复资源面板"
    session_response = await integration_client.post(
        "/api/v1/sessions",
        json={"title": session_title},
    )
    assert session_response.status_code == 200, session_response.text

    port_block = integration_port_block_for_file(Path(request.node.fspath))
    gateway = start_gateway_process(
        workspace_root=Path(integration_workspace_root_path),
        default_backend_url=f"http://127.0.0.1:{integration_backend_port}",
        port=port_block.port(20),
        extra_env={
            "BOXTEAM_WEB_ASSETS": str(
                project_root / "src" / "clients" / "web" / "dist"
            ),
        },
    )
    artifacts = Path(integration_workspace_root_path).parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "gateway-auth-resource-recovery-result.json"
    screenshot_path = artifacts / "gateway-auth-resource-recovery-failure.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    try:
        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                "BOXTEAM_BROWSER_SESSION_TITLE": session_title,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        browser_result = await asyncio.to_thread(
            subprocess.run,
            [
                "node",
                "tests/integration/clients/web/gateway_auth_resource_recovery.mjs",
            ],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
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
        assert result["noPageErrors"] is True
    finally:
        close_gateway_process(gateway)
