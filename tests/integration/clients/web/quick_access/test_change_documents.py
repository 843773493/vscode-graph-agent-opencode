"""真实执行生成更改，正式 Workbench 验只读模型、原 Diff 和刷新恢复。"""

import asyncio
import hashlib
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


async def test_diff_and_source_models_share_references_and_restore(
    request: pytest.FixtureRequest, browser_backend: tuple[str, Path, ModelStreamReplay], web_workbench_assets: Path,
) -> None:
    backend, workspace, replay = browser_backend
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "只读模型验收需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    (workspace / "AGENTS.md").write_text("只读来源模型验收\n", encoding="utf-8")
    before = "".join(f"history v1 line {index:02d}\n" for index in range(1, 21))
    after = before.replace("v1", "v2")
    replies = [
        {"tool_calls": [{"index": 0, "id": "create-history", "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps({"file_path": "diff-history.txt", "content": before, "expected_version": "absent"}),
        }}]}, {"content": "首版已生成"},
        {"tool_calls": [{"index": 0, "id": "edit-history", "type": "function", "function": {
            "name": "edit_file", "arguments": json.dumps({"file_path": "diff-history.txt", "old_string": "v1", "new_string": "v2",
                "replace_all": True, "expected_version": "sha256:" + hashlib.sha256(before.encode()).hexdigest()}),
        }}]}, {"content": "第二版已生成"},
    ]
    interactions = [cast(list[object], build_cassette(asset_id="change-documents", model="browser-chat-tool-loop-stub",
        url=f"http://127.0.0.1:{ports.port(10)}/v1/chat/completions",
        frames=chat_completion_frames(response_id=f"change-documents-{index}", model="browser-chat-tool-loop-stub",
            deltas=[{"role": "assistant", **reply}], finish_reason="tool_calls" if "tool_calls" in reply else "stop"),
    ).raw["interactions"])[0] for index, reply in enumerate(replies)]
    gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspace, default_backend_url=backend,
        port=ports.port(20), artifacts_dir=context.artifacts_dir / "gateway", extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    command = ["bun", "tests/integration/clients/web/quick_access/change_documents.mjs"]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    supervised_command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                          "--timeout-seconds", "240", "--", *command]
    result_path = context.artifacts_dir / "change-documents.json"
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            completed = await asyncio.to_thread(subprocess.run, supervised_command, cwd=context.project_root,
                env={**os.environ, **temporary, **replay.environment, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                    "BOXTEAM_CHANGE_INTERACTIONS": json.dumps(interactions), "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                    "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(context.artifacts_dir / "failure.png")},
                capture_output=True, text=True, timeout=270, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(completed.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(completed.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
            "supervised_command": supervised_command, "exit_code": completed.returncode}) + "\n")
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        result = json.loads(result_path.read_text())
        for key in ("singleDiffRead", "splitShared", "readonly", "patch", "legacyMigrated", "diffRestored", "sourceRestored", "lastCloseRereads",
                    "editorGroupAndMru", "editorDirtyClose", "editorCloseIsolated", "textSymbolsAfterThread"):
            assert result[key], key
        assert result["pageErrors"] == []
        assert result["existingWorkspaceViews"] == ["terminal", "output", "automation", "ports", "mcp", "browser"]
        assert (workspace / "diff-history.txt").read_text(encoding="utf-8") == after
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
