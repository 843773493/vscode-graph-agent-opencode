"""正式 Workbench／Gateway 的源码调试与原 Agent 工具共享运行。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import commentjson
import httpx
import psutil
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式 Workbench 产物
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    management_runtime as management_runtime,  # noqa: PLC0414 - HEAD 旧工作台组件
)
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


@pytest.fixture(autouse=True)
def debug_workbench_runtime(management_runtime, runtime_manifest_path: Path) -> None:
    _context, component, runtime = management_runtime
    manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
    manifest["extension_host"] = {"node_executable": str(runtime.executable), "component_root": str(component)}
    runtime_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def debug_model(request: pytest.FixtureRequest) -> Iterator[ModelStreamReplay]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with model_stream_replay(context.artifacts_dir,
        runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="workbench-debug-agent-shared-run",
    ) as replay:
        yield replay


@pytest.fixture
def debug_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    lease, probes = E2EProcessLease.acquire_available(
        context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json",
        range(ports.port(20), ports.end + 1),
    )
    try:
        (context.artifacts_dir / "port-allocation.json").write_text(
            json.dumps({"port": lease.ports[0], "probes": probes}), encoding="utf-8",
        )
        yield lease.ports[0]
    finally:
        lease.release()


def _model_interactions() -> list[dict[str, object]]:
    calls = [
        ("skill_load", {"name": "debugging"}),
        ("search_tools", {"tool_ids": ["get_variables_values", "evaluate_expression"], "limit": 10}),
        ("invoke_extension_tool", {"tool_name": "get_variables_values", "arguments": {"variableNames": ["state"], "scope": "local"}}),
        ("invoke_extension_tool", {"tool_name": "evaluate_expression", "arguments": {"expression": "state.counter += 10"}}),
    ]
    interactions = []
    for index in range(len(calls) + 1):
        if index < len(calls):
            name, arguments = calls[index]
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"debug-ui-agent-{index}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}]}
        else:
            delta = {"role": "assistant", "content": "DEBUG_SHARED_RUN_OK"}
        asset = build_cassette(asset_id=f"debug-ui-agent-{index}", model="debug-ui-model",
            url="https://opencode.ai/zen/v1/chat/completions",
            frames=chat_completion_frames(response_id=f"chatcmpl-debug-ui-{index}", model="debug-ui-model",
                deltas=(delta,), finish_reason="tool_calls" if index < len(calls) else "stop"))
        encoded = cast(list[dict[str, object]], asset.raw["interactions"])
        interactions.append(encoded[0])
    return interactions


def test_workbench_debug_ui_and_agent_share_plugin_run(
    request: pytest.FixtureRequest, web_workbench_assets: Path, debug_model: ModelStreamReplay,
    debug_gateway_port: int,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "Debug Workbench 验收需要真实 Chromium"
    port = debug_gateway_port
    base_url = f"http://127.0.0.1:{port}"
    workspace = prepare_test_workspace(workspace_root=context.workspace_root,
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace", template_items=())
    install_test_workspace_config(workspace_root=workspace,
        config_path=context.project_root / "configs/tests/workspace/default.jsonc",
        schema_path=context.project_root / "configs/workspace_schema.jsonc")
    configuration = workspace / ".boxteam/workspace.jsonc"
    source = commentjson.loads(configuration.read_text(encoding="utf-8"))
    source["llm"]["providers"] = [{"id": "debug-ui", "endpoint": "https://opencode.ai/zen/v1", "model": "debug-ui-model",
        "api_key": "debug-ui-test-key", "custom_llm_provider": "openai",
        "api_mode": {"protocol": "chat_completions", "model_info": {"supports_function_calling": True, "supports_reasoning": False},
            "supports_reasoning": {"reasoning_content": False}}}]
    for agent in source["agents"].values():
        if "model" in agent:
            agent["model"] = {"primary_provider": "debug-ui", "fallback_providers": []}
        if "custom" in agent["tools"]:
            agent["tools"]["custom"] = [tool for tool in agent["tools"]["custom"]
                if tool.get("name") in {"get_variables_values", "evaluate_expression"}]
    source.setdefault("runtime", {}).setdefault("gateway", {})["connection"] = {"url": base_url, "timeout_seconds": 5}
    configuration.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (workspace / "debug-ui.mjs").write_text(
        "const state = { counter: 1 };\nstate.counter += 2;\nstate.counter += 3;\nconsole.log('debug-result', state.counter);\nsetInterval(() => {}, 1000);\n",
        encoding="utf-8",
    )
    archive = context.artifacts_dir / "developer-tools.zip"
    subprocess.run(["bun", "scripts/plugins/pack-developer-tools.mjs", "--out", str(archive)],
        check=True, capture_output=True, text=True, timeout=60)
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None, port=port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        asgi_app="tests.support.e2e_control.gateway_app:app",
        extra_env={**debug_model.environment, "BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
    try:
        with httpx.Client(base_url=base_url, headers={"X-Local-Token": "local-dev-token"}, timeout=30, trust_env=False) as client:
            health = client.get("/api/gateway/health"); health.raise_for_status()
            installed = client.post("/api/gateway/service-plugins/install", timeout=600, json={
                "gateway_id": health.json()["data"]["gateway_id"], "path": str(archive.resolve())})
            installed.raise_for_status()
        supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
        command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
            "bun", "tests/integration/clients/web/debug_plugin_workbench.mjs"]
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            result = subprocess.run(command, cwd=context.project_root, timeout=195, check=False, capture_output=True, text=True,
                env={**os.environ, **debug_model.environment, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                    "BOXTEAM_DEBUG_UI_INPUT": json.dumps({"baseUrl": base_url, "artifacts": str(context.artifacts_dir),
                        "modelInteractions": _model_interactions()})})
        (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
        (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}), encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads((context.artifacts_dir / "debug-ui.json").read_text(encoding="utf-8"))
        assert receipt["phase"] == "complete" and receipt["pageErrors"] == []
        assert receipt["initialBreakpoint"] and receipt["stepOver"] and receipt["agentSharedRun"] and receipt["actualExit"]
        assert isinstance(receipt["pid"], int) and not psutil.pid_exists(receipt["pid"]), receipt
        assert not psutil.pid_exists(receipt["parallelThread"]["pid"]), receipt
    finally:
        close_gateway_process(gateway)
