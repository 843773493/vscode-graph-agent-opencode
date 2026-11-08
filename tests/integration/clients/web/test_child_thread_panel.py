"""模型回放与真实 Gateway/Agent/task 工具的子 Thread 工作台集成。"""

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
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式 Workbench 产物
)
from tests.support.model_stream.config import prepare_e2e_model_stream_config
from tests.support.paths import chromium_temporary_environment, output_root_for_test
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
    terminate_process,
)
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture(scope="module")
def integration_workspace_root_path(request: pytest.FixtureRequest) -> str:
    project_root = Path.cwd().resolve()
    output_root = output_root_for_test(
        Path(request.node.fspath),
        test_layer="integration",
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
def integration_browser_manager_processes(
    integration_workspace_root_path: str,
) -> Generator[BrowserManagerProcesses, None, None]:
    workspace_root = Path(integration_workspace_root_path).resolve()
    ports = integration_port_block_for_file(Path(__file__))
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
def integration_startup_mount(
    integration_workspace_root_path: str,
    integration_browser_manager_processes: BrowserManagerProcesses,
) -> MountedWorkspace:
    workspace_root = Path(integration_workspace_root_path).resolve()
    ports = integration_port_block_for_file(Path(__file__))
    return MountedWorkspace(
        load_or_create_workspace_id(workspace_root),
        workspace_root,
        browser_backend_url=f"http://127.0.0.1:{integration_browser_manager_processes.backend_port}",
    )


@pytest.fixture(scope="module")
def integration_backend_process(
    request: pytest.FixtureRequest,
    integration_startup_mount: MountedWorkspace,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    integration_backend_port: int,
):
    assert Path(integration_workspace_config_path).is_file()
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    replay_config = prepare_e2e_model_stream_config(
        Path.cwd() / "configs/tests/model_stream/model_stream_web_child_thread_delegate.jsonc",
        output_root=context.run_root,
        mode="replay",
    )
    gateway_port = integration_port_block_for_file(Path(request.node.path)).port(22)
    backend = start_backend_process(
        startup_mount=integration_startup_mount,
        workspace_root=integration_workspace_root_path,
        port=integration_backend_port,
        log_name="child-thread-integration-backend",
        env_overrides={
            "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{gateway_port}",
            "BOXTEAM_TEST_MODEL_STREAM_CONFIG": str(replay_config),
            # 模型请求使用显式回放，仍经过真实 Agent 与 task 工具。
            "OPENCODE_ZEN_API_KEY": "model-stream-replay-test-key",
        },
    )
    try:
        yield backend.process
    finally:
        close_backend_process(backend)


@pytest.mark.asyncio
async def test_child_thread_panel_delegate_flow_through_real_web_surface(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_backend_process: object,
    integration_backend_port: int,
    web_workbench_assets: Path,
) -> None:
    del integration_backend_process
    project_root = Path.cwd().resolve()
    workspace_root = Path(integration_workspace_root_path).resolve()
    chromium_path = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if chromium_path is None:
        pytest.fail("子会话线程面板 Web Integration 需要 Chromium")

    port = integration_port_block_for_file(Path(request.node.fspath)).port(22)
    backend_url = f"http://127.0.0.1:{integration_backend_port}"
    gateway = start_gateway_process(
        workspace_root=workspace_root,
        default_backend_url=backend_url,
        port=port,
        extra_env={
            "BOXTEAM_WEB_ASSETS": str(web_workbench_assets)
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
            owner = workspaces.json()["data"]["active_workspace"]
            workspace_id = owner["workspace_id"]
            gateway_id = owner["gateway_id"]

        environment = os.environ.copy()
        environment.update(
            {
                "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
                "BOXTEAM_BROWSER_GATEWAY_ID": gateway_id,
                "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium_path,
            }
        )
        scope = TestRunContext.from_test_file(Path(request.node.path)).prepare()
        with chromium_temporary_environment(scope.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary_environment:
            browser_result = await asyncio.to_thread(
                subprocess.run,
                ["bun", "tests/integration/clients/web/child_thread_panel.mjs"],
                cwd=project_root,
                env={**environment, **temporary_environment},
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        (artifacts / "browser-process.json").write_text(json.dumps({"exit_code": browser_result.returncode}) + "\n", encoding="utf-8")
        (artifacts / "browser.stdout.log").write_text(browser_result.stdout, encoding="utf-8")
        (artifacts / "browser.stderr.log").write_text(browser_result.stderr, encoding="utf-8")
        assert browser_result.returncode == 0, (
            "子会话线程面板 Web Integration 失败:\n"
            f"stdout:\n{browser_result.stdout}\n"
            f"stderr:\n{browser_result.stderr}\n"
            f"结果: {result_path}\n截图: {screenshot_path}"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["classification"] == "Integration"
        assert result["navigation"]["reusedTabDebugVisible"] is True
        assert result["sessionId"], "浏览器脚本未创建会话"

        # parent 第一轮：纯文本确认回复。
        assert result["confirmTurn"]["job"] in {"completed", "succeeded"}
        assert result["confirmTurn"]["finalText"] == "收到，会话运行正常，我可以继续执行任务。"

        # parent 第二轮：task 工具真实委派 + 最终文本。
        assert result["delegateTurn"]["jobStatus"] in {"completed", "succeeded"}
        assert result["delegateTurn"]["finalText"] == "子代理任务已完成。"
        assert "已运行 task" in result["delegateTurn"]["toolText"]

        # main Thread 的「运行与连接」资源面板展示 child 项。
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
        assert persisted["sessionLlmRequestCount"] == 5
        assert persisted["parentLlmRequestCount"] == 3
        assert persisted["childLlmRequestCount"] == 1
        assert persisted["reportLlmRequestCount"] == 1
        assert persisted["childSeed"]["task"] == "完成示例任务并输出结果"
        assert persisted["report"]["childThreadId"] == child["childThreadId"]
        assert persisted["report"]["finalText"] == "已收到子线程执行结果。"
        role_prefix = ["system", "user", "user"]
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
