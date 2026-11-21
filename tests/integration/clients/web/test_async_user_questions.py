"""正式 Workbench、Gateway、后端与精确模型回放的留言及重排交互。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    chat_backend as chat_backend,  # noqa: PLC0414
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    chat_backend_boxteam_home as chat_backend_boxteam_home,  # noqa: PLC0414
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    chat_loop_web_assets as chat_loop_web_assets,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.terminal_plugin import copy_prepared_developer_tools_package


async def test_questions_and_queue_reorder_through_real_workbench(chat_backend, chat_backend_boxteam_home, chat_loop_web_assets, prepared_developer_tools_package, request):
    backend_url, workspace, replay = chat_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    copy_prepared_developer_tools_package(prepared_developer_tools_package, chat_backend_boxteam_home)
    ports = integration_port_block_for_file(Path(request.node.path))
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=backend_url,
        port=ports.port(20), boxteam_home=chat_backend_boxteam_home,
        artifacts_dir=context.artifacts_dir / "gateway", extra_env={"BOXTEAM_WEB_ASSETS": str(chat_loop_web_assets)})
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "需要真实 Chromium"
    result_path = context.artifacts_dir / "browser-result.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "120", "--",
               "node", "tests/integration/clients/web/async_user_questions.mjs"]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            result = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root, capture_output=True, text=True, timeout=135, check=False,
                env={**os.environ, **temporary, **replay.environment, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                    "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}", "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                    "BOXTEAM_BROWSER_MODEL_ENDPOINT": f"http://127.0.0.1:{ports.port(10)}/v1/chat/completions",
                    "BOXTEAM_BROWSER_ARTIFACT_ROOT": str(context.artifacts_dir)})
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
    (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}, indent=2), encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    actual = json.loads(result_path.read_text())
    assert actual["phase"] == "complete" and actual["pageErrors"] == []
    assert actual["collapsedBodyReads"] == 0
    assert actual["questionsAfterDelete"] == 1
    assert actual["order"] == ["B 用户回答", "A 普通消息"]
    assert actual["consumedOrder"] == ["B 用户回答", "A 普通消息"]
