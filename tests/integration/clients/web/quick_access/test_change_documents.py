"""真实执行生成更改，正式 Workbench 验只读模型、原 Diff 和刷新恢复。"""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import ExitStack
from pathlib import Path
from typing import cast

import commentjson
import httpx
import pytest

from app.core.workspace_identity import read_workspace_id
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    close_backend_process,
    start_backend_process,
)
from tests.support.terminal_plugin import (
    copy_prepared_developer_tools_package,
    enable_developer_tools_plugins,
)


@pytest.fixture(scope="module")
def change_document_backend(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> Generator[tuple[str, Path, ModelStreamReplay, Path, Callable[[], None]], None, None]:
    """为本测试冻结同一端口块中的 Gateway 控制面地址和模型回放。"""
    port_block = integration_port_block_for_file(Path(request.node.fspath))
    workspace = Path(integration_workspace_root_path).resolve()
    config_path = Path(integration_workspace_config_path)
    gateway_url = f"http://127.0.0.1:{port_block.port(20)}"
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    provider = next(item for item in config["llm"]["providers"] if item["id"] == "primary")
    provider.update({
        "endpoint": f"http://127.0.0.1:{port_block.port(10)}/v1",
        "model": "browser-chat-tool-loop-stub",
        "api_key": "${BOXTEAM_TEST_MODEL_API_KEY}",
        "custom_llm_provider": "openai",
    })
    config.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = gateway_url
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    boxteam_home = context.runtime_root_for_node(request.node.nodeid) / "boxteam-home"
    with model_stream_replay(
        context.artifacts_dir,
        runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="real-workbench-changeset-and-diff-navigation",
    ) as replay, ExitStack() as services:
        lease = E2EProcessLease(
            context.runtime_root_for_node(request.node.nodeid) / "service-ports.json",
            tuple(port_block.port(offset) for offset in (0, 10, 20)),
        )
        lease.acquire()
        services.callback(lease.release)
        backend = start_backend_process(
            workspace_root=str(workspace),
            boxteam_home=boxteam_home,
            port=port_block.port(0),
            log_name="change-documents-backend",
            asgi_app="tests.support.e2e_control.backend_app:app",
            artifacts_dir=context.artifacts_dir,
            env_overrides={
                "BOXTEAM_TEST_MODEL_API_KEY": "e2e-local-model-key",
                "BOXTEAM_GATEWAY_URL": gateway_url,
                "BOXTEAM_RUNTIME_CONTROL_TOKEN": LOCAL_TOKEN_HEADERS["X-Local-Token"],
                **replay.environment,
            },
        )
        backend_closed = False

        def close_backend() -> None:
            nonlocal backend_closed
            if not backend_closed:
                close_backend_process(backend)
                backend_closed = True

        services.callback(close_backend)
        yield f"http://127.0.0.1:{backend.port}", workspace, replay, boxteam_home, close_backend


async def test_diff_and_source_models_share_references_and_restore(
    request: pytest.FixtureRequest,
    change_document_backend: tuple[str, Path, ModelStreamReplay, Path, Callable[[], None]],
    web_workbench_assets: Path,
    prepared_developer_tools_package: Path,
) -> None:
    backend, workspace, replay, boxteam_home, close_backend = change_document_backend
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
    copy_prepared_developer_tools_package(prepared_developer_tools_package, boxteam_home)
    gateway = await asyncio.to_thread(start_gateway_process, workspace_root=workspace, default_backend_url=backend,
        port=ports.port(20), boxteam_home=boxteam_home, artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    command = ["node", "tests/integration/clients/web/quick_access/change_documents.mjs"]
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    supervised_command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304",
                          "--timeout-seconds", "240", "--", *command]
    result_path = context.artifacts_dir / "change-documents.json"
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=40) as client:
            health = (await client.get('/api/gateway/health')).raise_for_status().json()['data']
            await enable_developer_tools_plugins(client, gateway_id=health['gateway_id'],
                workspace_id=read_workspace_id(workspace), plugin_ids=('terminal', 'browser'))
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
                    "sourceSplitShared", "editorGroupAndMru", "editorDirtyClose", "editorCloseIsolated", "textSymbolsAfterThread",
                    "changesetBrowseOnly", "changesetOpened"):
            assert result[key], key
        assert result["pageErrors"] == []
        assert result["existingWorkspaceViews"] == ["terminal", "output", "automation", "ports", "mcp", "browser"]
        assert (workspace / "diff-history.txt").read_text(encoding="utf-8") == after
    finally:
        try:
            close_backend()
        finally:
            await asyncio.to_thread(close_gateway_process, gateway)
