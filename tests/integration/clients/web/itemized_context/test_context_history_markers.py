"""真实 Workbench 打断、续发及恢复后的上下文标记和冻结正文。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import commentjson
import pytest

from app.prompting.validation import parse_root_section
from app.schemas.internal_v2.turn_search import ThreadItemInspectionRequest
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.mapping.itemized.history_turns.item_content import (
    context_source_contents,
)
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
from tests.support.ports import integration_ports_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.terminal_plugin import copy_prepared_developer_tools_package


@pytest.fixture(scope="module", autouse=True)
def marker_instructions(integration_workspace_root_path, integration_workspace_config_path):
    # 配置可辨认的真实系统来源；内部 user 来源沿原工具目录 producer 产生。
    (Path(integration_workspace_root_path) / "AGENTS.md").write_text(
        "# 上下文历史验收\n\nCONTEXT_MARKER_FROZEN_GUIDANCE：保留来源的完整原文。\n", encoding="utf-8")
    path = Path(integration_workspace_config_path)
    config = commentjson.loads(path.read_text(encoding="utf-8"))
    config["agents"]["default"]["instructions"]["system_prompt"] = "CONTEXT_MARKER_SYSTEM：完整保留系统来源。"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def test_interrupt_continue_and_reopen_context_markers(
    chat_backend, chat_backend_boxteam_home, chat_loop_web_assets, prepared_developer_tools_package, request,
):
    backend_url, workspace, replay = chat_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    copy_prepared_developer_tools_package(prepared_developer_tools_package, chat_backend_boxteam_home)
    ports = integration_ports_for_file(Path(request.node.path))
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=backend_url,
        port=ports.port(20), boxteam_home=chat_backend_boxteam_home,
        artifacts_dir=context.artifacts_dir / "gateway", extra_env={"BOXTEAM_WEB_ASSETS": str(chat_loop_web_assets)})
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "需要真实 Chromium"
    result_path = context.artifacts_dir / "browser-result.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
               "node", "tests/integration/clients/web/itemized_context/context_history_markers.mjs"]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            result = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root, capture_output=True,
                text=True, timeout=195, check=False, env={**os.environ, **temporary, **replay.environment,
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                    "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                    "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                    "BOXTEAM_BROWSER_MODEL_ENDPOINT": f"http://127.0.0.1:{ports.port(10)}/v1/chat/completions",
                    "BOXTEAM_BROWSER_ARTIFACT_ROOT": str(context.artifacts_dir)})
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
    (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps(
        {"command": command, "exit_code": result.returncode}, indent=2), encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    actual = json.loads(result_path.read_text())
    assert actual["phase"] == "complete" and actual["pageErrors"] == []
    assert len(actual["opened"]) == 3
    assert len(actual["copied"]) == 1
    assert len(actual["apiPerformance"]["samples"]) == 20
    owner = actual["owner"]
    first_call = actual["initial"][0]["context_model_call_id"]
    capture = json.loads((replay.artifacts / f"{first_call}.json").read_text())
    injected = {}
    for message in capture["request"]["messages"]:
        if not isinstance(message.get("content"), str) or not message["content"].startswith('<context_sources '):
            continue
        packet = parse_root_section(message["content"], tag="context_sources")
        assert packet["protocol"] == "boxteam-context-sources:v3"
        injected.update({source["source_identity"]: (message["role"], source["body"]) for source in packet["sources"]})
    assert injected["instruction:agent-instructions"] == ("system", "CONTEXT_MARKER_SYSTEM：完整保留系统来源。")
    assert injected["mcp-tool-guidance:v1"][0] == "user"
    assert "CONTEXT_MARKER_FROZEN_GUIDANCE" in injected["agents:workspace"][1]
    # 逐一核真实系统／内部 user 来源的冻结正文，不能只证明有两个标记。
    # 独立冷读原封存 owner，核编辑器显示的全文，不能用页面自身的 inspect 响应当预期。
    with RolloutCheckpointSaver(workspace / ".boxteam/sessions") as cold:
        with cold._storage.storage._connect(owner["thread_id"], "", session_id=owner["session_id"], read_only=True) as connection:
            for timing in actual["timings"]:
                if "modelCallId" in timing:
                    row = connection.execute("SELECT created_at FROM model_calls WHERE model_call_id=?", (timing["modelCallId"],)).fetchone()
                    assert row is not None
                    timing["callCreatedAt"] = row[0]
                    timing["elapsedMs"] = timing["visibleAtUnixMs"] - datetime.fromisoformat(row[0]).timestamp() * 1000
        result_path.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for timing in actual["timings"]:
            if "budgetMs" in timing:
                assert 0 <= timing["elapsedMs"] <= timing["budgetMs"], timing
        initial = {part["context_source_id"]: part["context_content_ref"] for part in actual["initial"]}
        for source_id in ("instruction:agent-instructions", "mcp-tool-guidance:v1", "agents:workspace"):
            reference = initial[source_id]
            body = cold.read_context_contribution(owner["session_id"], owner["thread_id"],
                historical_owner=(reference["session_id"], reference["thread_id"]),
                assembly_id=reference["assembly_id"], contribution_id=reference["contribution_id"])
            assert body == injected[source_id][1]
        for opened in [*actual["opened"], *actual["copied"]]:
            reference = opened["reference"]
            if reference["kind"] == "sealed_contribution":
                body = cold.read_context_contribution(owner["session_id"], owner["thread_id"],
                    historical_owner=(reference["session_id"], reference["thread_id"]),
                    assembly_id=reference["assembly_id"], contribution_id=reference["contribution_id"])
                expected = context_source_contents(body)[0].content
            else:
                content = cold.inspect_history_item(owner["session_id"], owner["thread_id"],
                    ThreadItemInspectionRequest(operation="content", item_id=reference["item_id"]))
                expected = content.contents[0].content
            assert opened["text"] == expected
