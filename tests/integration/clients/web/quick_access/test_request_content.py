"""真实执行生成请求日志，经 Gateway 搜索并打开原详情。"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    browser_backend as browser_backend,  # noqa: PLC0414
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process


async def test_request_content_search_opens_only_the_exact_record(
    request: pytest.FixtureRequest, browser_backend: tuple[str, Path, ModelStreamReplay], web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "请求日志验收需要 Chromium"
    backend_url, workspace, replay = browser_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port_block = integration_port_block_for_file(Path(request.node.path))
    gateway = await asyncio.to_thread(start_gateway_process,
        workspace_root=workspace, default_backend_url=backend_url, port=port_block.port(20),
        artifacts_dir=context.artifacts_dir / "gateway", extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    model = "browser-chat-tool-loop-stub"
    interaction = build_cassette(asset_id="request-content", model=model,
        url=f"http://127.0.0.1:{port_block.port(10)}/v1/chat/completions",
        frames=chat_completion_frames(response_id="request-content", model=model,
            deltas=[{"role": "assistant", "content": "已完成请求日志验收"}], finish_reason="stop"),
    ).raw["interactions"][0]
    result_path = context.artifacts_dir / "request-content.json"
    command = ["bun", "tests/integration/clients/web/quick_access/request_content.mjs"]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, **replay.environment,
                     "BOXTEAM_REQUEST_SEARCH_INTERACTION": json.dumps(interaction),
                     "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "request-content-failure.png"),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=150, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": completed.returncode}) + "\n")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text())
        for key in ("unselectedHasNoContentQuery", "exactRecordLocated", "samePaneNewLocation", "staleReferenceRejected", "noFullLogRead"):
            assert receipt[key], key
        assert receipt["pageErrors"] == []
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
