"""真实 Gateway／正式 Workbench 的消息正文发现与原文定位。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

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


@pytest.mark.asyncio
async def test_message_content_search_opens_unloaded_child_and_exact_original_range(
    request: pytest.FixtureRequest,
    browser_backend: tuple[str, Path, ModelStreamReplay],
    web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if not chromium:
        pytest.fail("消息正文验收需要 Chromium")
    backend_url, workspace, replay = browser_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=backend_url,
        port=integration_port_block_for_file(Path(request.node.path)).port(20),
        artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)},
    )
    result_path = context.artifacts_dir / "message-content.json"
    command = ["bun", "tests/integration/clients/web/quick_access/message_content.mjs"]
    model = "browser-chat-tool-loop-stub"
    endpoint = integration_port_block_for_file(Path(request.node.path)).port(10)
    interactions = [build_cassette(
        asset_id="message-content", model=model, url=f"http://127.0.0.1:{endpoint}/v1/chat/completions",
        frames=chat_completion_frames(response_id=f"message-content-{index}", model=model,
                                      deltas=[{"role": "assistant", "content": text}], finish_reason="stop"),
    ).raw["interactions"][0] for index, text in enumerate(["助手独有😀正文", *[f"已完成 {index}" for index in range(6)]])]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(
                subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, **replay.environment,
                     "BOXTEAM_MESSAGE_SEARCH_INTERACTIONS": json.dumps(interactions),
                     "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium},
                capture_output=True, text=True, timeout=210, check=False,
            )
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": completed.returncode}) + "\n")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        receipt = json.loads(result_path.read_text())
        for key in ("unselectedHasNoContentQuery", "unopenedChildLocated", "longOriginalRangeVisible",
                    "reusedPaneRevealsAnotherMatch", "staleReferenceRejected", "fileUsesOriginalProvider"):
            assert receipt[key] is True, key
        assert receipt["pageErrors"] == []
    finally:
        close_gateway_process(gateway)
