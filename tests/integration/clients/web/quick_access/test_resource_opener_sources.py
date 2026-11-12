"""真实执行返回规范工具结果引用，原 Opener／只读编辑器验证打开及恢复。"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from app.core.workspace_identity import read_workspace_id
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


async def test_canonical_tool_result_opens_in_original_readonly_editor(
    request: pytest.FixtureRequest, browser_backend: tuple[str, Path, ModelStreamReplay], web_workbench_assets: Path,
) -> None:
    backend, workspace, replay = browser_backend
    workspace_id = read_workspace_id(workspace)
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "规范来源打开验收需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    (workspace / "AGENTS.md").write_text("资源打开归属基线\n", encoding="utf-8")
    replies = [{"tool_calls": [{"index": 0, "id": "source-tool-output", "type": "function", "function": {
        "name": "invoke_extension_tool", "arguments": json.dumps({"tool_name": "large_test_output", "arguments": {}}),
    }}]}, {"content": "完整工具结果已生成"}]
    interactions = [cast(list[object], build_cassette(asset_id="resource-opener-sources", model="browser-chat-tool-loop-stub",
        url=f"http://127.0.0.1:{ports.port(10)}/v1/chat/completions",
        frames=chat_completion_frames(response_id=f"resource-opener-sources-{index}", model="browser-chat-tool-loop-stub",
            deltas=[{"role": "assistant", **reply}], finish_reason="tool_calls" if "tool_calls" in reply else "stop"),
    ).raw["interactions"])[0] for index, reply in enumerate(replies)]
    gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspace, default_backend_url=backend,
        port=ports.port(20), artifacts_dir=context.artifacts_dir / "gateway", extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    command = ["node", "tests/integration/clients/web/quick_access/resource_opener_sources.mjs"]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    supervised_command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                          "--timeout-seconds", "180", "--", *command]
    result_path = context.artifacts_dir / "resource-opener-sources.json"
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(subprocess.run, supervised_command, cwd=context.project_root,
                env={**os.environ, **temporary, **replay.environment, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                    "BOXTEAM_SOURCE_INTERACTIONS": json.dumps(interactions), "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                    "BOXTEAM_BROWSER_WORKSPACE_ID": workspace_id,
                    "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "failure.png")},
                capture_output=True, text=True, timeout=210, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
            "supervised_command": supervised_command, "exit_code": completed.returncode}) + "\n")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        result = json.loads(result_path.read_text())
        assert result["owner"]["workspaceId"] == workspace_id
        assert result["owner"]["gatewayId"]
        assert all((source["gatewayId"], source["workspaceId"]) == (
            result["owner"]["gatewayId"], workspace_id,
        ) for source in result["sources"])
        for key in ("toolArtifactOpened", "toolArtifactReadonly", "toolArtifactSplitShared", "toolArtifactRestored",
                    "toolArtifactReopen", "unsupportedSourceRejected", "noExecutionOrFileFallback",
                    "sourceReferenceCopied", "toolReferenceCopied", "fileReferenceCopiedWithoutOpen"):
            assert result[key], key
        assert result["pageErrors"] == []
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
