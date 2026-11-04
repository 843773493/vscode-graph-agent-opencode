"""产品执行与 fork 生成多页历史，验证原非文本大纲及组内预览。"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    browser_backend as browser_backend,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process


async def test_thread_outline_pages_preview_cancel_and_accept(
    request: pytest.FixtureRequest, browser_backend: tuple[str, Path, ModelStreamReplay],
    web_workbench_assets: Path, search_gateway_port: int,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "Thread 大纲验收需要 Chromium"
    backend_url, workspace, replay = browser_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspace,
        default_backend_url=backend_url, port=search_gateway_port, artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    model = "browser-chat-tool-loop-stub"
    endpoint = integration_port_block_for_file(Path(request.node.path)).port(10)
    interactions = [cast(list[object], build_cassette(asset_id="thread-outline", model=model,
        url=f"http://127.0.0.1:{endpoint}/v1/chat/completions", frames=chat_completion_frames(
            response_id=f"thread-outline-{index}", model=model,
            deltas=[{"role": "assistant", "content": f"已完成第 {index} 次执行\n" + "大纲定位验收内容\n" * 16}],
            finish_reason="stop")).raw["interactions"])[0] for index in range(24)]
    result_path = context.artifacts_dir / "thread-outline.json"
    command = ["bun", "tests/integration/clients/web/quick_access/thread_outline.mjs"]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    supervised_command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                          "--timeout-seconds", "720", "--", *command]
    try:
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
