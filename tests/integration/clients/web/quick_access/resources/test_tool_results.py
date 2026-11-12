"""真实 ToolResult 的 Thread Resource Quick Access 页面与定位验收。"""

from __future__ import annotations

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
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 复用正式 Workbench 构建。
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    chat_backend as chat_backend,  # noqa: PLC0414 - 复用真实 Backend 与 model_stream 控制器。
)
from tests.support.model_stream.config import ModelStreamReplay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)


@pytest.mark.asyncio
async def test_real_child_tool_result_quick_access_locates_original_tool_row(
    request: pytest.FixtureRequest,
    chat_backend: tuple[str, Path, ModelStreamReplay],
    web_workbench_assets: Path,
) -> None:
    backend_url, workspace, replay = chat_backend
    chromium = (
        os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    assert chromium, "工具结果 Quick Access 页面验收需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    gateway = start_gateway_process(
        workspace_root=workspace,
        default_backend_url=backend_url,
        port=port_block.port(20),
        artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)},
    )
    result_path = context.artifacts_dir / "tool-results-browser.json"
    screenshot_path = context.artifacts_dir / "tool-results-browser.png"
    command = ["node", "tests/integration/clients/web/quick_access/resources/tool_results.mjs"]
    supervisor = context.project_root / "app/runtime" / (
        "windows_job.py" if sys.platform == "win32" else "posix_job.py"
    )
    supervised_command = [
        sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
        "--timeout-seconds", "180", "--", *command,
    ]
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=20,
        ) as client:
            health_response = await client.get("/api/gateway/health")
            assert health_response.status_code == 200, health_response.text
            health = health_response.json()["data"]
            workspaces_response = await client.get("/api/gateway/workspaces?check_health=false")
            assert workspaces_response.status_code == 200, workspaces_response.text
            workspace_id = workspaces_response.json()["data"]["active_workspace"]["workspace_id"]
            assert workspaces_response.json()["data"]["active_workspace"]["gateway_id"] == health["gateway_id"]

        fixture = {
            "gatewayId": health["gateway_id"],
            "workspaceId": workspace_id,
            "toolName": "large_test_output",
        }
        model = "browser-chat-tool-loop-stub"
        endpoint = port_block.port(10)
        replies = [
            ({"tool_calls": [{
                "index": 0,
                "id": "tool-result-quick-access-call",
                "type": "function",
                "function": {
                    "name": "invoke_extension_tool",
                    "arguments": json.dumps({"tool_name": "large_test_output", "arguments": {}}),
                },
            }]}, "tool_calls"),
            ({"content": "工具结果 Quick Access 已完成。"}, "stop"),
        ]
        fixture["interactions"] = cast(list[dict[str, object]], [
            build_cassette(
                asset_id="tool-result-quick-access",
                model=model,
                url=f"http://127.0.0.1:{endpoint}/v1/chat/completions",
                frames=chat_completion_frames(
                    response_id=f"tool-result-quick-access-{index}",
                    model=model,
                    deltas=[{"role": "assistant", **reply}],
                    finish_reason=finish_reason,
                ),
            ).raw["interactions"][0]
            for index, (reply, finish_reason) in enumerate(replies)
        ])

        environment = {
            **os.environ,
            **replay.environment,
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
            "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
            "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
            "BOXTEAM_BROWSER_SCREENSHOT_PATH": str(screenshot_path),
            "BOXTEAM_BROWSER_FIXTURE": json.dumps(fixture, ensure_ascii=False),
        }
        with chromium_temporary_environment(
            context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp",
        ) as temporary:
            completed = await asyncio.to_thread(
                subprocess.run,
                supervised_command,
                cwd=context.project_root,
                env={**environment, **temporary},
                capture_output=True,
                text=True,
                timeout=210,
                check=False,
            )
        (context.artifacts_dir / "tool-results-browser.stdout.raw").write_text(completed.stdout, encoding="utf-8")
        (context.artifacts_dir / "tool-results-browser.stderr.raw").write_text(completed.stderr, encoding="utf-8")
        (context.artifacts_dir / "tool-results-browser-process.json").write_text(
            json.dumps({"command": command, "supervised_command": supervised_command,
                        "exit_code": completed.returncode}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}\n{result_path}"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert result["phase"] == "complete"
        assert result["browseOnly"] is True
        assert result["locatedOriginalToolRow"] is True
        assert result["noReplayOnAccept"] is True
        assert result["toolLists"]
        assert all(item["gatewayId"] == health["gateway_id"] for item in result["toolLists"])
        assert all(item["workspaceId"] == workspace_id for item in result["toolLists"])
        assert result["pageErrors"] == []
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
