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


@pytest.fixture(scope="module")
def e2e_model_stream_config_path() -> str:
    return str(Path.cwd() / "configs" / "tests" / "model_stream_web_basic_chat_tool_loop.jsonc")


@pytest.mark.asyncio
async def test_basic_chat_tool_loop_through_real_web_surface(
    request: pytest.FixtureRequest,
    e2e_workspace_root_path: str,
    e2e_backend_process: object,
    e2e_backend_port: int,
) -> None:
    del e2e_backend_process
    project_root = Path.cwd().resolve()
    workspace_root = Path(e2e_workspace_root_path).resolve()
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("基础聊天工具循环 Web E2E 需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    port = e2e_port_block_for_file(Path(request.node.fspath)).port(20)
    backend_url = f"http://127.0.0.1:{e2e_backend_port}"
    gateway = start_gateway_process(
        workspace_root=workspace_root,
        default_backend_url=backend_url,
        port=port,
        extra_env={
            "BOXTEAM_WEB_ASSETS": str(project_root / "src" / "clients" / "web" / "dist")
        },
    )
    artifacts = workspace_root.parent / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    result_path = artifacts / "basic-chat-tool-loop-result.json"
    screenshot_path = artifacts / "basic-chat-tool-loop-failure.png"
    result_path.unlink(missing_ok=True)
    screenshot_path.unlink(missing_ok=True)
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=60,
        ) as client:
            await acquire_gateway_guest(client)
            workspaces = await client.get("/api/gateway/workspaces")
            assert workspaces.status_code == 200, workspaces.text
            workspace_id = workspaces.json()["data"]["active_workspace_id"]
            assert isinstance(workspace_id, str) and workspace_id

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        browser_result = await asyncio.to_thread(
            subprocess.run,
            ["node", "tests/e2e/clients/web/basic-chat-tool-loop.e2e.mjs"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert browser_result.returncode == 0, (
            "基础聊天工具循环 Web E2E 失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["firstJob"]["status"] in {"completed", "succeeded"}
        assert result["secondJob"]["status"] in {"completed", "succeeded"}
        assert result["firstTurn"]["userCount"] == 1
        assert result["firstTurn"]["finalText"] == "首轮工具调用已完成。"
        assert "已运行 read_file" in result["firstTurn"]["toolText"]
        assert result["restoredHistory"]["finalText"] == "首轮工具调用已完成。"
        assert result["secondTurn"]["userCount"] == 1
        assert result["secondTurn"]["finalText"] == "第二轮工具调用也已完成。"
        assert "已运行 read_file" in result["secondTurn"]["toolText"]
        assert result["streams"] == {"trace": 2, "message": 2}
        assert result["persisted"]["llmRequestCount"] == 4
        assert result["diagnostics"] == {
            "pageErrors": [],
            "consoleErrors": [],
            "failedRequests": [],
        }
    finally:
        close_gateway_process(gateway)
