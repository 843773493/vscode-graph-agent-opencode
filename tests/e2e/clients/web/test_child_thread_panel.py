from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.browser_manager import (
    BrowserManagerProcesses,
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.paths import output_root_for_test
from tests.support.ports import e2e_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import terminate_process
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture(scope="module")
def e2e_workspace_root_path(request: pytest.FixtureRequest) -> str:
    project_root = Path.cwd().resolve()
    output_root = output_root_for_test(
        Path(request.node.fspath),
        test_layer="e2e",
        project_root=project_root,
    )
    workspace_root = output_root / "workspace"
    reset_gateway_persistent_state(workspace_root=workspace_root)
    workspace = prepare_test_workspace(
        workspace_root=workspace_root,
        template_root=(
            project_root
            / "tests"
            / "fixtures"
            / "workspaces"
            / "default_test_workspace"
        ),
        template_items=(".boxteam/workspace-identity.json",),
    )
    return str(workspace)


@pytest.fixture(scope="module")
def e2e_model_stream_config_path() -> str:
    return str(
        Path.cwd()
        / "configs"
        / "tests"
        / "model_stream"
        / "model_stream_web_child_thread_delegate.jsonc"
    )


@pytest.fixture(scope="module")
def e2e_terminal_backend(
    e2e_workspace_root_path: str,
) -> Generator[subprocess.Popen[str], None, None]:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    terminal_port = e2e_port_block_for_file(Path(__file__)).port(40)
    terminal_process = start_terminal_backend(
        backend_port=terminal_port,
        workspace_root=str(workspace_root),
        artifacts_dir=TestRunContext.from_test_file(Path(__file__)).artifacts_dir,
    )
    try:
        yield terminal_process
    finally:
        terminate_process(terminal_process)


@pytest.fixture(scope="module")
def e2e_browser_manager_processes(
    e2e_workspace_root_path: str,
) -> Generator[BrowserManagerProcesses, None, None]:
    workspace_root = Path(e2e_workspace_root_path).resolve()
    ports = e2e_port_block_for_file(Path(__file__))
    browser_processes = start_browser_manager_processes(
        workspace_root=workspace_root,
        artifacts_dir=TestRunContext.from_test_file(Path(__file__)).artifacts_dir,
        backend_port=ports.port(50),
        frontend_port=ports.port(51),
        backend_host="127.0.0.1",
    )
    try:
        yield browser_processes
    finally:
        close_browser_manager_processes(browser_processes)


@pytest.fixture(scope="module")
def e2e_startup_mount(
    e2e_workspace_root_path: str,
    e2e_terminal_backend: subprocess.Popen[str],
    e2e_browser_manager_processes: BrowserManagerProcesses,
) -> MountedWorkspace:
    del e2e_terminal_backend
    workspace_root = Path(e2e_workspace_root_path).resolve()
    ports = e2e_port_block_for_file(Path(__file__))
    return MountedWorkspace(
        load_or_create_workspace_id(workspace_root),
        workspace_root,
        terminal_backend_url=f"http://127.0.0.1:{ports.port(40)}",
        browser_backend_url=f"http://127.0.0.1:{e2e_browser_manager_processes.backend_port}",
    )


@pytest.fixture(scope="module")
def e2e_backend_composition() -> tuple[str, dict[str, str]]:
    gateway_port = e2e_port_block_for_file(Path(__file__)).port(22)
    return "app.main:app", {
        "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{gateway_port}",
        # strict replay 拦截模型请求；占位 key 只满足 Provider 初始化，不访问真实服务。
        "OPENCODE_ZEN_API_KEY": "model-stream-replay-test-key",
    }


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
    artifacts = TestRunContext.from_test_file(Path(__file__)).artifacts_dir
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
        assert child["statusText"] == "运行中"
        assert "general-purpose" in child["metaText"]

        # 点击 child 项切换当前 Session 内的 Node Debug owner。
        navigation = result["navigation"]
        assert navigation["selectedThreadId"] == child["childThreadId"]
        assert navigation["debugOwnerVisible"] is True

        # 会话日志按 Job id 区分父会话请求与子线程请求。
        persisted = result["persisted"]
        assert persisted["sessionLlmRequestCount"] == 4
        assert persisted["parentLlmRequestCount"] == 3
        assert persisted["childLlmRequestCount"] == 1
        role_prefix = ["system", "user", "user", "user", "user", "user"]
        assert persisted["parentUpstreamMessageRoles"][0] == role_prefix
        assert persisted["parentUpstreamMessageRoles"][1] == role_prefix + [
            "assistant",
            "user",
        ]
        third_call_roles = persisted["parentUpstreamMessageRoles"][2]
        assert third_call_roles in (
            role_prefix + ["assistant", "user", "assistant", "tool"],
            role_prefix + ["assistant", "user", "assistant"],
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
