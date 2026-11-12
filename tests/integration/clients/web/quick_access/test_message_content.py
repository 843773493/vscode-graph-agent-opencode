"""真实 Gateway／正式 Workbench 的消息正文发现与原文定位。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.agents.tools.testing import LARGE_TEST_OUTPUT, LARGE_TEST_TARGET_VALUE
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


@pytest.mark.asyncio
async def test_message_content_search_opens_unloaded_child_and_exact_original_range(
    request: pytest.FixtureRequest,
    chat_backend: tuple[str, Path, ModelStreamReplay],
    chat_backend_boxteam_home: Path,
    prepared_developer_tools_package: Path,
    web_workbench_assets: Path,
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    if not chromium:
        pytest.fail("消息正文验收需要 Chromium")
    backend_url, workspace, replay = chat_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    copy_prepared_developer_tools_package(prepared_developer_tools_package, chat_backend_boxteam_home)
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=backend_url,
        boxteam_home=chat_backend_boxteam_home,
        port=integration_port_block_for_file(Path(request.node.path)).port(20),
        artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)},
    )
    result_path = context.artifacts_dir / "message-content.json"
    command = ["node", "tests/integration/clients/web/quick_access/message_content.mjs"]
    model = "browser-chat-tool-loop-stub"
    endpoint = integration_port_block_for_file(Path(request.node.path)).port(10)
    interactions = [cast(list[dict[str, object]], build_cassette(
        asset_id="message-content", model=model, url=f"http://127.0.0.1:{endpoint}/v1/chat/completions",
        frames=chat_completion_frames(response_id=f"message-content-{index}", model=model,
                                      deltas=[{"role": "assistant", "content": text}], finish_reason="stop"),
    ).raw["interactions"])[0] for index, text in enumerate(["助手独有😀正文", *[f"已完成 {index}" for index in range(6)]])]
    interactions.insert(0, cast(list[dict[str, object]], build_cassette(
        asset_id="tool-content", model=model, url=f"http://127.0.0.1:{endpoint}/v1/chat/completions",
        frames=chat_completion_frames(response_id="tool-content", model=model, deltas=[{
            "role": "assistant", "tool_calls": [{"index": 0, "id": "content-output", "type": "function",
                "function": {"name": "invoke_extension_tool", "arguments": json.dumps({
                    "tool_name": "large_test_output", "arguments": {},
                })}}],
        }], finish_reason="tool_calls"),
    ).raw["interactions"])[0])
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}",
                                     headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            response = await client.get("/api/gateway/workspaces?check_health=false")
            response.raise_for_status()
            owner = response.json()["data"]["active_workspace"]
            await enable_developer_tools_plugins(client, gateway_id=owner["gateway_id"],
                                                 workspace_id=owner["workspace_id"], plugin_ids=("terminal", "browser"))
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(
                subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, **replay.environment,
                     "BOXTEAM_MESSAGE_SEARCH_INTERACTIONS": json.dumps(interactions),
                     "BOXTEAM_TOOL_SEARCH_QUERY": LARGE_TEST_TARGET_VALUE,
                     "BOXTEAM_TOOL_SEARCH_SHA256": hashlib.sha256(LARGE_TEST_OUTPUT.encode()).hexdigest(),
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
                    "reusedPaneRevealsAnotherMatch", "staleReferenceRejected", "fileUsesOriginalProvider",
                    "unopenedToolChildLocated", "fullToolOutputVisible", "toolCategoryIsolated",
                    "multiCategorySelectionIsolated", "cancelKeepsContentCategories", "noExecutionDuringCategorySelection"):
            assert receipt[key] is True, key
        assert receipt["pageErrors"] == []
    finally:
        close_gateway_process(gateway)
