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
    return str(
        Path.cwd()
        / "configs"
        / "tests"
        / "model_stream"
        / "model_stream_web_child_thread_delegate.jsonc"
    )


@pytest.mark.asyncio
async def test_child_thread_panel_delegate_flow_through_real_web_surface(
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
        pytest.fail("子会话线程面板 Web E2E 需要 Chromium")

    build = await asyncio.to_thread(
        subprocess.run,
        ["bun", "run", "build"],
        cwd=project_root / "src" / "clients" / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, f"Web 构建失败:\n{build.stdout}\n{build.stderr}"

    port = e2e_port_block_for_file(Path(request.node.fspath)).port(22)
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
    result_path = artifacts / "child-thread-panel-result.json"
    screenshot_path = artifacts / "child-thread-panel-failure.png"
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
            ["node", "tests/e2e/clients/web/child-thread-panel.e2e.mjs"],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert browser_result.returncode == 0, (
            "子会话线程面板 Web E2E 失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["sessionId"], "浏览器脚本未创建会话"

        # parent 第一轮：纯文本确认回复。
        assert result["confirmTurn"]["job"] in {"completed", "succeeded"}
        assert result["confirmTurn"]["finalText"] == "收到，会话运行正常，我可以继续执行任务。"

        # parent 第二轮：task 工具真实委派 + 最终文本。
        assert result["delegateTurn"]["jobStatus"] in {"completed", "succeeded"}
        assert result["delegateTurn"]["finalText"] == "子代理任务已完成。"
        assert "已运行 task" in result["delegateTurn"]["toolText"]

        # 右侧侧边栏「运行与连接」标签中的子会话线程面板展示 child 项。
        child = result["childThread"]
        assert child["childThreadId"], "面板未提供 child thread_id"
        assert child["title"].startswith("委派：")
        assert "完成示例任务并输出结果" in child["title"]
        assert child["statusText"] == "等待启动"
        assert "general-purpose" in child["metaText"]

        # 点击 child 项切换当前 Session 内的 Node Debug owner。
        navigation = result["navigation"]
        assert navigation["selectedThreadId"] == child["childThreadId"]
        assert navigation["debugOwnerVisible"] is True

        # 后端持久化证据：parent 请求与 child-threads 列表一致。
        # 注意：委派工具结果后的 parent 第 3 次请求存在产品侧非确定性——
        # 消息列表可能带或不带尾部 tool 结果消息（见 R7 报告），两种形态都被 fixture 覆盖。
        persisted = result["persisted"]
        assert persisted["parentLlmRequestCount"] == 3
        third_call_roles = persisted["parentUpstreamMessageRoles"][2]
        assert third_call_roles in (
            ["system", "user", "assistant", "user", "assistant", "tool"],
            ["system", "user", "assistant", "user", "assistant"],
        )
        assert len(persisted["childThreads"]) == 1
        assert persisted["childThreads"][0]["thread_id"] == child["childThreadId"]

        assert result["diagnostics"] == {
            "pageErrors": [],
            "consoleErrors": [],
            "failedRequests": [],
        }
    finally:
        close_gateway_process(gateway)
