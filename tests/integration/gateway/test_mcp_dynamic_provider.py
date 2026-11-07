"""正式 Workbench／真实 VSIX Provider／原 SDK 与 Thread 的动态定义纵向验收。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from zipfile import ZipFile

import commentjson
import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    InstallationCase,
    installation_route,
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    installation_case as installation_case,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import prepare_default_test_workspace

PRIVATE_TOKEN = "dynamic-provider-memory-only-test-token"


@pytest.fixture(autouse=True)
def provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # 原进程环境提供本次测试秘密；不把它写进 VSIX、JSONC 或 fixture 源码。
    monkeypatch.setenv("BOXTEAM_PROVIDER_PRIVATE_TOKEN", PRIVATE_TOKEN)
    monkeypatch.setenv("BOXTEAM_PROVIDER_PYTHON", sys.executable)
    monkeypatch.setenv("BOXTEAM_PROVIDER_SERVER", str(Path.cwd() / "tests/integration/workspace_services/mcp/private_env_server.py"))


@pytest.fixture
def secondary_case(installation_case: InstallationCase, request: pytest.FixtureRequest,
        web_workbench_assets: Path) -> Iterator[InstallationCase]:
    case = installation_case
    workspace = prepare_default_test_workspace(workspace_root=case.context.workspace_root / (request.node.name + "-B"),
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", fresh_identity=True)
    shutil.copyfile(case.workspace / ".boxteam/workspace.jsonc", workspace / ".boxteam/workspace.jsonc")
    home = case.context.boxteam_home_for_node(request.node.nodeid + "-B")
    port = integration_port_block_for_file(Path(request.node.path)).port(1)
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None, port=port,
        boxteam_home=home, artifacts_dir=case.context.artifacts_dir / "gateway-b",
        asgi_app="tests.support.e2e_control.gateway_app:app", extra_env={**case.replay.environment,
            "BOXTEAM_WEB_ASSETS": str(web_workbench_assets),
            "BOXTEAM_RUNTIME_MANIFEST": os.environ["BOXTEAM_TEST_MCP_RUNTIME_MANIFEST"]})
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=LOCAL_TOKEN_HEADERS, timeout=30, trust_env=False) as client:
            client.post("/api/gateway/users/guest", json={"tracking": {"test": "provider-B"}}).raise_for_status()
            yield InstallationCase(client, case.context, workspace, home, case.replay)
    finally:
        close_gateway_process(gateway)


def _provider_vsix(workspace: Path) -> Path:
    target = workspace / "dynamic-provider.vsix"
    manifest = {"name": "dynamic-provider", "publisher": "boxteam-probe", "version": "1.0.0", "type": "module",
        "engines": {"vscode": "^1.0.0"}, "main": "./extension.mjs", "extensionKind": ["workspace"],
        "activationEvents": ["onStartupFinished"], "contributes": {
            "mcpServerDefinitionProviders": [{"id": "definitions", "label": "Dynamic Provider"}],
            "commands": [{"command": f"provider-probe.{name}", "title": f"Provider {name}"}
                for name in ("state", "change", "dispose", "hold", "release", "hold-state")]}}
    code = """
import * as vscode from 'vscode';
export function activate(context) {
  const changes = new vscode.EventEmitter(), resolved = [];
  const originals = [0, 1].map(index => new vscode.McpStdioServerDefinition('Duplicate definition',
    '/must-not-use-provide-command', [String(index)], { TOKEN: process.env.BOXTEAM_PROVIDER_PRIVATE_TOKEN }, 'same-version'));
  let provided = 0, held = false, cancelled = 0, returned = 0, release;
  const registration = vscode.lm.registerMcpServerDefinitionProvider('definitions', {
    onDidChangeMcpServerDefinitions: changes.event,
    provideMcpServerDefinitions() { provided++; return originals; },
    async resolveMcpServerDefinition(original, token) {
      if (token.isCancellationRequested) throw new Error('resolve cancelled');
      const index = originals.indexOf(original);
      if (index < 0) throw new Error('resolve did not receive the original object');
      resolved.push(index);
      if (held) {
        const listener = token.onCancellationRequested(() => cancelled++);
        await new Promise(resolve => { release = resolve; });
        listener.dispose(); returned++;
      }
      return new vscode.McpStdioServerDefinition('Resolved definition', process.env.BOXTEAM_PROVIDER_PYTHON,
        [process.env.BOXTEAM_PROVIDER_SERVER, 'definition-' + index, '', '--literal="quoted value"'], {
          MCP_PRIVATE_TOKEN: process.env.BOXTEAM_PROVIDER_PRIVATE_TOKEN,
          MCP_ENV_RECEIPTS: vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, 'provider-receipts.jsonl').fsPath,
          MCP_PLAIN_VALUE: 'definition-' + index, MCP_EMPTY_VALUE: '', MCP_SKIPPED_VALUE: null });
    }
  });
  context.subscriptions.push(changes, registration,
    vscode.commands.registerCommand('provider-probe.state', () => ({ provided, resolved: [...resolved] })),
    vscode.commands.registerCommand('provider-probe.hold', () => { held = true; }),
    vscode.commands.registerCommand('provider-probe.release', () => { held = false; release?.(); }),
    vscode.commands.registerCommand('provider-probe.hold-state', () => ({ held, cancelled, returned })),
    vscode.commands.registerCommand('provider-probe.change', () => changes.fire()),
    vscode.commands.registerCommand('provider-probe.dispose', () => registration.dispose()));
}
"""
    with ZipFile(target, "w") as archive:
        archive.writestr("extension/package.json", json.dumps(manifest))
        archive.writestr("extension/extension.mjs", code)
    return target


def _operation(case: InstallationCase, base: str, server: str, action: str) -> dict:
    key = f"provider-{action}-{server}"
    response = case.client.post(base + "/connection-operations", json={"operation_id": key, "server_id": server, "action": action})
    response.raise_for_status()
    deadline = time.monotonic() + 20
    while True:
        response = case.client.get(base + "/connection-operations/" + key)
        response.raise_for_status()
        result = response.json()["data"]
        if result["status"] in {"succeeded", "failed", "expired", "cancelled"}:
            return result
        assert time.monotonic() < deadline, result
        time.sleep(0.05)


async def test_real_vsix_duplicate_objects_target_isolation_late_resolve_and_offline_reference(
    installation_case: InstallationCase,
    secondary_case: InstallationCase,
) -> None:
    case = installation_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "动态 Provider 验收需要真实 Chromium"
    assert os.getenv("BOXTEAM_TEST_MCP_RUNTIME_MANIFEST"), "动态 Provider 验收需要匹配当前源码的原 Node component"
    target, _ = installation_route(case.client)
    listing = case.client.get("/api/gateway/workspaces").json()["data"]
    workspace_id = listing["active_workspace"]["workspace_id"]
    vsix = _provider_vsix(case.workspace)
    response = case.client.post("/api/gateway/extension-installations/install", json={"protocol_version": 1,
        "gateway_id": target["gateway_id"], "backend_id": target["backend_id"], "workspace_ids": [workspace_id],
        "source_uri": f"boxteam://workspace/{workspace_id}/resources/files/root/{vsix.name}"})
    assert response.status_code == 200, response.text
    installed = response.json()
    assert installed["request_id"] == response.headers["X-Request-ID"]
    assert installed["data"]["installed"]["id"] == "boxteam-probe.dynamic-provider"
    (case.context.artifacts_dir / "provider-install.json").write_text(json.dumps(installed, indent=2), encoding="utf-8")
    secondary_target, _ = installation_route(secondary_case.client)
    secondary_workspace_id = secondary_case.client.get("/api/gateway/workspaces").json()["data"]["active_workspace"]["workspace_id"]
    secondary_vsix = secondary_case.workspace / vsix.name
    shutil.copyfile(vsix, secondary_vsix)
    assert secondary_vsix.read_bytes() == vsix.read_bytes(), "两个 Gateway 必须安装同一 VSIX 字节"
    response = secondary_case.client.post("/api/gateway/extension-installations/install", json={"protocol_version": 1,
        "gateway_id": secondary_target["gateway_id"], "backend_id": secondary_target["backend_id"],
        "workspace_ids": [secondary_workspace_id],
        "source_uri": f"boxteam://workspace/{secondary_workspace_id}/resources/files/root/{secondary_vsix.name}"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["installed"]["id"] == installed["data"]["installed"]["id"]
    (case.context.artifacts_dir / "provider-install-b.json").write_text(response.text, encoding="utf-8")
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
        "node", "tests/integration/clients/web/mcp_dynamic_provider.mjs"]
    runtime = case.context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"])
    with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, timeout=195, capture_output=True, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_MCP_DYNAMIC_PROVIDER": json.dumps({"baseUrl": str(case.client.base_url).rstrip("/"),
                    "secondaryBaseUrl": str(secondary_case.client.base_url).rstrip("/"),
                    "artifacts": str(case.context.artifacts_dir), "privateToken": PRIVATE_TOKEN})})
    (case.context.artifacts_dir / "browser.stdout.raw").write_bytes(result.stdout)
    (case.context.artifacts_dir / "browser.stderr.raw").write_bytes(result.stderr)
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
        "exit_code": result.returncode, "memory_limit_kb": 4194304, "timeout_seconds": 180}, indent=2), encoding="utf-8")
    assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="backslashreplace")
    source_path = case.workspace / ".boxteam/workspace.jsonc"
    saved_bytes = source_path.read_bytes()
    servers = commentjson.loads(saved_bytes.decode("utf-8"))["mcp"]["servers"]
    first, second = (servers[f"dynamic-{index}"] for index in (0, 1))
    assert all(set(server) == {"enabled", "transport", "provider_definition"} for server in (first, second))
    assert first["provider_definition"]["definition_ref"] != second["provider_definition"]["definition_ref"]
    assert first["provider_definition"]["provider_ref"] == second["provider_definition"]["provider_ref"]
    assert PRIVATE_TOKEN.encode() not in saved_bytes

    def invoke(current: InstallationCase, indices: tuple[int, ...], scope: str) -> dict:
        response = current.client.post("/api/v1/sessions", json={"title": f"动态 Provider 原对象工具验收 {scope}"})
        response.raise_for_status()
        session = response.json()["data"]
        calls = [{"index": index, "id": f"dynamic-call-{index}", "type": "function", "function": {
            "name": "invoke_extension_tool", "arguments": json.dumps({"tool_name": f"mcp__dynamic-{index}__private_env_probe",
                "arguments": {"proof": f"provider-{scope}-{index}", "include_inputs": True}})}} for index in indices]
        model_calls = current.replay.control.plan_model_calls(session["session_id"], session["thread_id"], [
            cast(list[dict[str, object]], build_cassette(asset_id=f"dynamic-provider-{scope.lower()}-{ordinal}", model="e2e-stub-model",
                url="https://opencode.ai/zen/v1/chat/completions", frames=chat_completion_frames(response_id=f"dynamic-provider-{ordinal}",
                    model="e2e-stub-model", deltas=({"role": "assistant", **reply},),
                    finish_reason="tool_calls" if "tool_calls" in reply else "stop")).raw["interactions"])[0]
            for ordinal, reply in enumerate(({"tool_calls": calls}, {"content": "动态 Provider 工具已核对。"}))])
        accepted = current.client.post(f"/api/v1/sessions/{session['session_id']}/messages", json={
            "message": {"content": "调用两个动态 Provider 原定义的工具。"}, "run": {"mode": "single_agent", "agent_id": "default"}})
        accepted.raise_for_status()
        deadline = time.monotonic() + 60
        while True:
            response = current.client.get(f"/api/v1/jobs/{accepted.json()['data']['job_id']}")
            response.raise_for_status()
            job = response.json()["data"]
            if job["status"] in {"succeeded", "completed"}:
                break
            assert job["status"] not in {"failed", "cancelled", "timed_out"} and time.monotonic() < deadline, job
            time.sleep(0.1)
        record = json.loads((current.replay.artifacts / f"{model_calls[-1]}.json").read_text(encoding="utf-8"))
        messages = {message["tool_call_id"]: message for message in record["request"]["messages"] if message.get("role") == "tool"}
        assert set(messages) == {f"dynamic-call-{index}" for index in indices}
        for index in indices:
            message = messages[f"dynamic-call-{index}"]
            assert message["name"] == "invoke_extension_tool"
            assert json.loads(message["content"][0]["text"]) == {"proof": f"provider-{scope}-{index}",
                "secret_digest": hashlib.sha256(PRIVATE_TOKEN.encode()).hexdigest(), "prefix": sys.prefix,
                "arguments": [f"definition-{index}", "", '--literal="quoted value"'],
                "environment": {"MCP_PLAIN_VALUE": f"definition-{index}", "MCP_EMPTY_VALUE": "", "MCP_SKIPPED_VALUE": None}}
        return {"session_id": session["session_id"], "model_call_ids": model_calls, "messages": messages}

    evidence = await asyncio.to_thread(invoke, case, (0, 1), "A")
    secondary_evidence = await asyncio.to_thread(invoke, secondary_case, (0,), "B")
    secondary_saved = (secondary_case.workspace / ".boxteam/workspace.jsonc").read_bytes()
    secondary_servers = commentjson.loads(secondary_saved.decode())["mcp"]["servers"]
    assert secondary_servers["foreign-provider"]["provider_definition"] == first["provider_definition"]
    secondary_ref = secondary_servers["dynamic-0"]["provider_definition"]
    assert secondary_ref["gateway_id"] != first["provider_definition"]["gateway_id"]
    assert secondary_ref["backend_id"] != first["provider_definition"]["backend_id"]
    assert secondary_ref["host_generation"] != first["provider_definition"]["host_generation"]
    for current, expected in ((case, 2), (secondary_case, 1)):
        receipts = [json.loads(line) for line in (current.workspace / "provider-receipts.jsonl").read_text().splitlines()]
        assert sum(item["kind"] == "started" for item in receipts) == expected, receipts
    assert servers["late-provider"]["enabled"] is True
    base = f"/api/v1/workspaces/{workspace_id}/mcp"
    assert (await asyncio.to_thread(_operation, case, base, "dynamic-0", "disconnect"))["status"] == "succeeded"
    reconnect = await asyncio.to_thread(_operation, case, base, "dynamic-0", "connect")
    assert reconnect["status"] == "failed", reconnect
    response = case.client.get(base + "/servers")
    response.raise_for_status()
    statuses = {server["server_id"]: server for server in response.json()["data"]}
    assert statuses["dynamic-0"]["status"] == "unavailable" and statuses["dynamic-0"]["unavailable_reason"]
    assert statuses["dynamic-0"]["provider_definition"] == first["provider_definition"]
    assert source_path.read_bytes() == saved_bytes
    assert (await asyncio.to_thread(_operation, case, base, "dynamic-1", "disconnect"))["status"] == "succeeded"
    secondary_base = f"/api/v1/workspaces/{secondary_workspace_id}/mcp"
    assert (await asyncio.to_thread(_operation, secondary_case, secondary_base, "dynamic-0", "disconnect"))["status"] == "succeeded"
    (case.context.artifacts_dir / "provider-thread-result.json").write_text(json.dumps({**evidence,
        "secondary_thread": secondary_evidence, "secondary_provider_ref": secondary_ref, "offline_reconnect": reconnect,
        "statuses": statuses}, indent=2), encoding="utf-8")
    for directory in (case.context.run_root, case.workspace, case.home, secondary_case.workspace, secondary_case.home):
        for path in directory.rglob("*"):
            if path.is_file():
                assert PRIVATE_TOKEN.encode() not in path.read_bytes(), str(path)
