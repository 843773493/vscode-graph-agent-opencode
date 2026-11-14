"""真实工具测试入口与恢复；模型 HTTP 边界复用统一 model_stream 资产。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import commentjson
import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.support.model_stream.config import prepare_e2e_model_stream_config
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@pytest.fixture
def tool_test_run_environment(request: pytest.FixtureRequest) -> Iterator[dict[str, str]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = context.project_root
    prepared = os.getenv("BOXTEAM_TOOLS_UI_ASSETS")
    assets = Path(prepared).resolve(strict=True) if prepared else request.getfixturevalue("web_workbench_assets")
    runtime = context.runtime_root_for_node(request.node.nodeid)
    ports = integration_port_block_for_file(Path(request.node.path))
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root / context.run_root.name,
        template_root=root / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True,
    )
    configuration = install_test_workspace_config(workspace_root=workspace,
        config_path=root / "configs/tests/workspace/default.jsonc", schema_path=root / "configs/workspace_schema.jsonc")
    payload = commentjson.loads(configuration.read_text())
    model, provider_id = "tool-test-replay-model", "tool-test-replay"
    endpoint = "https://tool-test.invalid/v1"
    payload["llm"]["providers"] = [{
        "id": provider_id, "model": model, "endpoint": endpoint, "api_key": "tool-test-unused-key",
        "custom_llm_provider": "openai", "api_mode": {"protocol": "chat_completions",
            "supports_reasoning": {"reasoning_content": False}, "model_info": {"supports_function_calling": True}},
    }]
    for agent in payload["agents"].values():
        agent["model"] = {"primary_provider": provider_id, "fallback_providers": []}
    for tool in payload["agents"]["default"]["tools"]["custom"]:
        if tool.get("tool_id") == "fetch_webpage":
            tool["options"]["embedding"]["provider_id"] = provider_id
    payload["mcp"] = {"servers": {}}
    configuration.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    fixture_root = runtime / "model-stream-fixture"
    (fixture_root / "scenarios").mkdir(parents=True, exist_ok=True)
    seed = root / "tests/fixtures/workspaces/model_tool_test_workspace/edit_file/exact_replacement/target.txt"
    # 固定响应只正确完成第一个正式 case；其余 case 应如实失败，验收真实成功率而非伪造全绿。
    arguments = {"file_path": "target.txt", "expected_version": "sha256:" + hashlib.sha256(seed.read_bytes()).hexdigest(),
        "old_string": 'status = "draft"\nowner = "local-agent"',
        "new_string": 'status = "ready"\nowner = "tool-test"', "replace_all": False}
    cassette = build_cassette(asset_id="workspace-tool-test-run", model=model, url=endpoint + "/chat/completions",
        frames=chat_completion_frames(response_id="tool-test-run", model=model,
            deltas=[{"role": "assistant", "tool_calls": [{"index": 0, "id": "tool-test-call", "type": "function",
                "function": {"name": "edit_file", "arguments": json.dumps(arguments)}}]}], finish_reason="tool_calls"))
    (fixture_root / "calls.json").write_text(json.dumps(cassette.raw) + "\n")
    (fixture_root / "scenarios/basic-chat-tool-loop.jsonc").write_text(json.dumps({
        "scenario_id": "basic-chat-tool-loop", "asset": "calls.json",
        "business_assertion": "tool-test-ui-start-and-reload-with-real-cases",
    }) + "\n")
    stream_config = prepare_e2e_model_stream_config(root / "configs/tests/model_stream/model_stream_web_basic_chat_tool_loop.jsonc",
        runtime_root=runtime, artifacts_dir=context.artifacts_dir)
    replay = json.loads(stream_config.read_text())
    replay["model_stream"]["transport"]["fixture_root"] = str(fixture_root)
    stream_config.write_text(json.dumps(replay) + "\n")
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None, port=ports.port(21),
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_DEFAULT_BACKEND_PORT": str(ports.port(20)), "BOXTEAM_WEB_ASSETS": str(assets),
            "BOXTEAM_TEST_MODEL_STREAM_CONFIG": str(stream_config), "LITELLM_LOCAL_MODEL_COST_MAP": "true"})
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
                timeout=90, trust_env=False) as client:
            active = client.get("/api/gateway/workspaces").raise_for_status().json()["data"]["active_workspace"]
        yield {"baseUrl": f"http://127.0.0.1:{gateway.port}", "gatewayId": active["gateway_id"],
            "workspaceId": active["workspace_id"], "providerId": provider_id,
            "artifacts": str(context.artifacts_dir), "runtime": str(runtime), "workspace": str(workspace),
            "mainSha256": hashlib.sha256((assets / "out/main.js").read_bytes()).hexdigest()}
    finally:
        close_gateway_process(gateway)


def test_workspace_tool_model_run_restores_original_run_after_reload(tool_test_run_environment: dict[str, str]) -> None:
    fixture = tool_test_run_environment
    artifacts = Path(fixture["artifacts"])
    node = shutil.which("node")
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert node and chromium, "工具测试界面验收需要目标 Node 和 Chromium"
    command = [node, "tests/integration/clients/web/workspace_tool_test_run.mjs"]
    with chromium_temporary_environment(Path(fixture["runtime"]) / "chromium-tmp") as temporary:
        run = subprocess.run(command, cwd=Path.cwd(), capture_output=True, text=True, check=False, timeout=150,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_TOOL_TEST_FIXTURE": json.dumps(fixture)})
    (artifacts / "browser.stdout.raw").write_text(run.stdout)
    (artifacts / "browser.stderr.raw").write_text(run.stderr)
    (artifacts / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": run.returncode,
        "main_sha256": fixture["mainSha256"]}) + "\n")
    assert run.returncode == 0, f"{run.stdout}\n{run.stderr}\n{artifacts}"
    result = json.loads((artifacts / "result.json").read_text())
    assert result["phase"] == "completed" and result["pageErrors"] == []
    assert len(result["starts"]) == 1 and result["run"]["status"] == "completed"
    saved = json.loads((Path(fixture["workspace"]) / ".boxteam/tool_tests/edit_file/run.json").read_text())
    assert saved["run_id"] == result["run"]["run_id"] and saved["attempts"] == result["run"]["attempts"]
