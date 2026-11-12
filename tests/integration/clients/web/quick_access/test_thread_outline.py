"""产品执行与 fork 生成多页历史，验证原非文本大纲及组内预览。"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    chat_backend as chat_backend,  # noqa: PLC0414
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    chat_backend_boxteam_home as chat_backend_boxteam_home,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.terminal_plugin import (
    copy_prepared_developer_tools_package,
    enable_developer_tools_plugins,
)


async def test_thread_outline_pages_preview_cancel_and_accept(
    request: pytest.FixtureRequest, chat_backend: tuple[str, Path, ModelStreamReplay],
    chat_backend_boxteam_home: Path,
    prepared_developer_tools_package: Path,
    web_workbench_assets: Path, search_gateway_port: int,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Thread 大纲验收需要 Chromium"
    backend_url, workspace, replay = chat_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    copy_prepared_developer_tools_package(prepared_developer_tools_package, chat_backend_boxteam_home)
    gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspace,
        default_backend_url=backend_url, port=search_gateway_port,
        artifacts_dir=context.artifacts_dir / "gateway", boxteam_home=chat_backend_boxteam_home,
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    model = "browser-chat-tool-loop-stub"
    endpoint = integration_port_block_for_file(Path(request.node.path)).port(10)
    interactions = [cast(list[object], build_cassette(asset_id="thread-outline", model=model,
        url=f"http://127.0.0.1:{endpoint}/v1/chat/completions", frames=chat_completion_frames(
            response_id=f"thread-outline-{index}", model=model,
            deltas=[{"role": "assistant", "content": f"已完成第 {index} 次执行\n" + "大纲定位验收内容\n" * 16}],
            finish_reason="stop")).raw["interactions"])[0] for index in range(24)]
    result_path = context.artifacts_dir / "thread-outline.json"
    command = ["node", "tests/integration/clients/web/quick_access/thread_outline.mjs"]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    supervised_command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                          "--timeout-seconds", "720", "--", *command]
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}",
                                     headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            response = await client.get("/api/gateway/workspaces?check_health=false")
            response.raise_for_status()
            owner = response.json()["data"]["active_workspace"]
            await enable_developer_tools_plugins(client, gateway_id=owner["gateway_id"],
                                                 workspace_id=owner["workspace_id"], plugin_ids=("terminal", "browser"))
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(subprocess.run, supervised_command, cwd=context.project_root,
                env={**os.environ, **temporary, **replay.environment,
                     "BOXTEAM_OUTLINE_INTERACTIONS": json.dumps(interactions),
                     "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "thread-outline-failure.png"),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=750, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
            "supervised_command": supervised_command, "exit_code": completed.returncode}) + "\n")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text())
        for key in ("coverageAndExplicitPage", "previewRestored", "previewNotPersisted", "groupsIsolated", "unloadedTurnAccepted",
                    "noBusinessSearch", "preciseChildHistory", "emptyThreadOutline"):
            assert receipt[key], key
        assert receipt["pageErrors"] == []
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
