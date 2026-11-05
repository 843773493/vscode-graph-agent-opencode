from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式 Workbench 构建
)
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process


@pytest.fixture
def thread_identity_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port = integration_port_block_for_file(Path(request.node.path)).port(20)
    lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", (port,))
    # 保留绑定，避免浏览器／模型目录请求的自动源端口在启动前抢占。
    lease.acquire()
    try:
        yield port
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_thread_editor_stable_owner_and_restore(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    thread_identity_gateway_port: int,
    web_workbench_assets: Path,
) -> None:
    del integration_workspace_config_path  # fixture 在 Gateway 启动前安装隔离工作区配置。
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if not chromium:
        pytest.fail("Thread 编辑器身份验收需要 Chromium")
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    result_path = context.artifacts_dir / "thread-editor-result.json"
    screenshot_path = context.artifacts_dir / "thread-editor-failure.png"
    gateway = start_gateway_process(
        workspace_root=Path(integration_workspace_root_path), default_backend_url=None,
        port=thread_identity_gateway_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir / "gateway",
        # 本场景不调用 Provider，不让在线模型目录刷新阻塞编辑器身份验收。
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
    )
    try:
        command = ["bun", "tests/integration/clients/web/thread_editor_identity.mjs"]
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            result = await asyncio.to_thread(
                subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary,
                     "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=150, check=False,
            )
        (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({
            "command": command, "exit_code": result.returncode,
        }) + "\n")
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text())
        assert receipt["stableOpenDeduplicated"] is True
        assert receipt["splitAndReloadPreserved"] is True
        assert receipt["unavailableRecordsPreserved"] is True
        assert receipt["removalPreservedSessionAndDraft"] is True
        assert receipt["unknownGatewayRejected"] is True
        assert receipt["ambiguousRouteRejected"] is True
        assert receipt["pageErrors"] == []
    finally:
        close_gateway_process(gateway)
