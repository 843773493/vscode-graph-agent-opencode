"""正式工作台收集运行输入、保存原配置，并在真实 Thread 验实际进程输入。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import commentjson
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    ArtifactIndex,
    InstallationCase,
    _wheel,
    installation_route,
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    artifact_download_gate as artifact_download_gate,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    artifact_index as artifact_index,  # noqa: PLC0414
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    installation_case as installation_case,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment

SECRET = "mcp-configuration-input-test-secret"
EXPECTED_INPUTS: dict[str, object] = {"--tag": ["red", "red", "blue"], "--boolean": "true",
    "--directory": "/native target/with spaces", "--empty": "", "label": "", "MCP_PLAIN_VALUE": "fast", "MCP_EMPTY_VALUE": ""}
EXPECTED_ARGUMENTS = ["serve", "--tag", "red", "--tag", "red", "--tag", "blue", "--port", "8123", "--boolean", "true",
    "--directory", "/native target/with spaces", "--empty", "", "--template", "prefix-"]


@pytest.fixture
def input_catalog(artifact_index: ArtifactIndex, installation_case: InstallationCase) -> ArtifactIndex:
    index, case = artifact_index, installation_case
    _wheel(index.root, "artifact-server", "1.0.0", (Path.cwd() / "tests/integration/workspace_services/mcp/private_env_server.py").read_bytes(),
        entrypoint="artifact-server")
    manifest = index.root / "v0.1/servers/org.example/artifact/versions/release-1"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    package, = document["server"]["packages"]
    package["packageArguments"] = [{"type": "positional", "value": "serve"},
        {"type": "named", "name": "--tag", "isRepeated": True, "isRequired": True, "choices": ["red", "blue"]},
        {"type": "named", "name": "--port", "format": "number", "default": "8123"},
        {"type": "named", "name": "--boolean", "format": "boolean", "isRequired": True},
        {"type": "named", "name": "--directory", "format": "filepath"},
        {"type": "named", "name": "--optional", "format": "string"},
        {"type": "named", "name": "--empty", "format": "string"},
        {"type": "named", "name": "--template", "value": "prefix-{label}", "variables": {"label": {}}}]
    package["environmentVariables"] = [
        {"name": "MCP_ENV_RECEIPTS", "value": str(case.workspace / "input-receipts.jsonl")},
        {"name": "MCP_PLAIN_VALUE", "default": "normal", "choices": ["normal", "fast"]},
        {"name": "MCP_EMPTY_VALUE", "format": "string"}, {"name": "MCP_SKIPPED_VALUE", "format": "string"},
        {"name": "MCP_PRIVATE_TOKEN", "isSecret": True, "isRequired": True}]
    manifest.write_text(json.dumps(document), encoding="utf-8")
    return index


def test_workbench_inputs_reach_original_thread_without_plaintext_secret(
    installation_case: InstallationCase, input_catalog: ArtifactIndex,
) -> None:
    case, index = installation_case, input_catalog
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "MCP 配置输入验收需要真实 Chromium"
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
        "node", "tests/integration/clients/web/mcp_configuration_inputs.mjs"]
    runtime = case.context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"])
    with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
        result = subprocess.run(command, timeout=195, capture_output=True, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_MCP_CONFIGURATION_INPUTS": json.dumps({"baseUrl": str(case.client.base_url).rstrip("/"),
                    "registryUrl": index.url, "artifacts": str(case.context.artifacts_dir), "expectedInputs": EXPECTED_INPUTS,
                    "secretKey": "MCP_PRIVATE_TOKEN", "secretValue": SECRET})})
    (case.context.artifacts_dir / "browser.stdout.raw").write_bytes(result.stdout)
    (case.context.artifacts_dir / "browser.stderr.raw").write_bytes(result.stderr)
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
        "exit_code": result.returncode, "memory_limit_kb": 4194304, "timeout_seconds": 180}, indent=2), encoding="utf-8")
    assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="backslashreplace")
    saved = commentjson.loads((case.workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))["mcp"]["servers"]["configured-inputs"]
    assert saved["enabled"] and saved["args"] == EXPECTED_ARGUMENTS
    assert saved["env"]["MCP_PRIVATE_TOKEN"]["kind"] == "mcp_private_env"
    assert saved["env"]["MCP_PLAIN_VALUE"] == "fast" and saved["env"]["MCP_EMPTY_VALUE"] == ""
    assert "MCP_SKIPPED_VALUE" not in saved["env"] and SECRET not in json.dumps(saved)
    session_response = case.client.post("/api/v1/sessions", json={"title": "真实参数与私有环境变量"})
    session_response.raise_for_status()
    session = session_response.json()["data"]
    replies = [{"tool_calls": [{"index": 0, "id": "configuration-input-probe", "type": "function", "function": {
        "name": "invoke_extension_tool", "arguments": json.dumps({"tool_name": "mcp__configured-inputs__private_env_probe",
            "arguments": {"proof": "workbench-inputs", "include_inputs": True}})}}]}, {"content": "已验证实际参数。"}]
    calls = case.replay.control.plan_model_calls(session["session_id"], session["thread_id"], [
        cast(list[dict[str, object]], build_cassette(asset_id=f"configuration-inputs-{ordinal}", model="e2e-stub-model",
            url="https://opencode.ai/zen/v1/chat/completions", frames=chat_completion_frames(response_id=f"configuration-inputs-{ordinal}",
                model="e2e-stub-model", deltas=({"role": "assistant", **reply},),
                finish_reason="tool_calls" if "tool_calls" in reply else "stop")).raw["interactions"])[0]
        for ordinal, reply in enumerate(replies)])
    accepted = case.client.post(f"/api/v1/sessions/{session['session_id']}/messages", json={
        "message": {"content": "调用 MCP 配置输入探针，验证实际目标进程。"}, "run": {"mode": "single_agent", "agent_id": "default"}})
    accepted.raise_for_status()
    deadline = time.monotonic() + 60
    while True:
        response = case.client.get(f"/api/v1/jobs/{accepted.json()['data']['job_id']}")
        response.raise_for_status()
        job = response.json()["data"]
        if job["status"] in {"succeeded", "completed"}:
            break
        assert job["status"] not in {"failed", "cancelled", "timed_out"} and time.monotonic() < deadline, job
        time.sleep(0.1)
    record = json.loads((case.replay.artifacts / f"{calls[-1]}.json").read_text(encoding="utf-8"))
    message, = [item for item in record["request"]["messages"] if item.get("tool_call_id") == "configuration-input-probe"]
    part, = message["content"]
    observed = json.loads(part["text"])
    _, installation_base = installation_route(case.client)
    inventory = case.client.get(installation_base + "/installations")
    inventory.raise_for_status()
    artifact, = inventory.json()["data"]
    assert artifact["binding"] == saved["managed_launch"]
    assert observed == {"proof": "workbench-inputs", "secret_digest": hashlib.sha256(SECRET.encode()).hexdigest(),
        "prefix": str(Path(artifact["command"]).parent.parent),
        "arguments": EXPECTED_ARGUMENTS, "environment": {"MCP_PLAIN_VALUE": "fast", "MCP_EMPTY_VALUE": "", "MCP_SKIPPED_VALUE": None}}
    assert message["name"] == "invoke_extension_tool" and SECRET not in json.dumps(record)
    receipts = [json.loads(line) for line in (case.workspace / "input-receipts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["kind"] == "called" and row["proof"] == "workbench-inputs" for row in receipts)
    (case.context.artifacts_dir / "thread-input-result.json").write_text(json.dumps({"tool_message": message,
        "session_id": session["session_id"], "thread_id": session["thread_id"], "model_call_ids": calls}, indent=2), encoding="utf-8")
