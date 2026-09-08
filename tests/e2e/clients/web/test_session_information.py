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
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import e2e_port_block_for_file


@pytest.mark.asyncio
async def test_copy_session_information_uses_bounded_protocol_projection(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
) -> None:
    project_root = Path.cwd().resolve()
    workspace_root = Path(e2e_workspace_root_path).resolve()
    output_root = workspace_root.parent
    artifacts = output_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "session-information-result.json"
    screenshot_path = artifacts / "session-information-failure.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)

    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("会话信息 Web E2E 需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    port = e2e_port_block_for_file(Path(request.node.fspath)).port(21)
    gateway = start_gateway_process(
        workspace_root=workspace_root,
        default_backend_url="http://127.0.0.1:9",
        port=port,
        extra_env={
            "BOXTEAM_WEB_ASSETS": str(
                project_root / "src" / "clients" / "web" / "dist"
            )
        },
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=30,
        ) as client:
            await acquire_gateway_guest(client)
            workspaces_response = await client.get("/api/gateway/workspaces")
            assert workspaces_response.status_code == 200, workspaces_response.text
            workspace_id = workspaces_response.json()["data"]["active_workspace_id"]
            create_response = await client.post(
                "/api/v1/sessions",
                json={"title": "超长诊断标题" * 300},
                headers={"X-BoxTeam-Workspace-Id": workspace_id},
            )
            assert create_response.status_code == 200, create_response.text
            session_id = create_response.json()["data"]["session_id"]

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_E2E_BASE_URL": f"http://127.0.0.1:{port}",
                "BOXTEAM_E2E_FIXTURE": json.dumps(
                    {"sessionId": session_id}, ensure_ascii=False
                ),
                "BOXTEAM_E2E_RESULT_PATH": str(result_path),
                "BOXTEAM_E2E_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        result = await asyncio.to_thread(
            subprocess.run,
            ["node", "tests/e2e/clients/web/session_information.mjs"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, (
            f"浏览器会话信息 E2E 失败:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}\n截图: {screenshot_path}"
        )
        browser_result = json.loads(result_path.read_text(encoding="utf-8"))
        assert browser_result["kind"] == "session_diagnostic_snapshot"
        assert browser_result["schemaVersion"] == 2
        assert browser_result["titleTruncated"] is True
        assert browser_result["titleLength"] == 512
        assert browser_result["activeResourceCount"] <= 32
        assert browser_result["recentClosedResourceCount"] <= 16
        assert browser_result["recentErrorCount"] <= 5
        assert browser_result["apiPayloadBytes"] < 50_000
        assert browser_result["clipboardBytes"] < 50_000
    finally:
        close_gateway_process(gateway)
