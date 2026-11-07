"""正式 Workbench 手动配置 stdio／HTTP，再由真实 Thread 验证目标私有输入。"""

from __future__ import annotations

import asyncio
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
from starlette.datastructures import Headers

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
from tests.integration.workspace_services.mcp.test_invocation_lifecycle import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment

ENV_SECRET = "manual-env-private-test-value"
HEADER_SECRET = "manual-header-private-test-value"
ARGUMENTS = ["", '--literal="quoted value"', "repeat", "repeat"]


def _disconnect_http(case: InstallationCase) -> None:
    """先结清真实 SDK SSE 连接，再关闭外部 HTTP 测试服务。"""
    listing = case.client.get("/api/gateway/workspaces")
    listing.raise_for_status()
    workspace_id = listing.json()["data"]["active_workspace"]["workspace_id"]
    base = f"/api/v1/workspaces/{workspace_id}/mcp"
    servers = case.client.get(base + "/servers")
    servers.raise_for_status()
    if not any(server["server_id"] == "manual-http" for server in servers.json()["data"]):
        return
    operation_id = "manual-http-cleanup"
    stopped = case.client.post(base + "/connection-operations", json={
        "operation_id": operation_id, "server_id": "manual-http", "action": "disconnect"})
    stopped.raise_for_status()
    deadline = time.monotonic() + 10
    while True:
        response = case.client.get(base + "/connection-operations/" + operation_id)
        response.raise_for_status()
        operation = response.json()["data"]
        if operation["status"] == "succeeded":
            return
        assert operation["status"] not in {"failed", "expired", "cancelled"} and time.monotonic() < deadline, operation
        time.sleep(0.05)


async def test_original_add_manual_servers_preserves_argv_private_inputs_and_unknown_save(
    installation_case: InstallationCase, http_schema_endpoint_factory,
) -> None:
    case = installation_case
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "手动 MCP 页面验收需要真实 Chromium"
    observations: list[dict[str, object]] = []

    def observe_call(name: str, arguments: dict[str, object], headers: Headers) -> None:
        observations.append({"name": name, "arguments": arguments,
            "private_header_matches": headers.get("X-Private-Token") == HEADER_SECRET,
            "plain_header_matches": headers.get("X-Plain-Test") == "ordinary-header"})

    async with http_schema_endpoint_factory("manual-http", required_headers={
        "X-Private-Token": HEADER_SECRET, "X-Plain-Test": "ordinary-header",
    }, call_observer=observe_call) as endpoint:
        try:
            supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
            command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180", "--",
                "node", "tests/integration/clients/web/mcp_manual_configuration.mjs"]
            runtime = case.context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"])
            working = case.workspace / "manual working directory"
            working.mkdir()
            script = Path.cwd() / "tests/integration/workspace_services/mcp/private_env_server.py"
            receipts = case.workspace / "manual-env-receipts.jsonl"
            with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
                result = await asyncio.to_thread(subprocess.run, command, timeout=195, capture_output=True, check=False,
                    env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                        "BOXTEAM_MCP_MANUAL_CONFIGURATION": json.dumps({"baseUrl": str(case.client.base_url).rstrip("/"),
                            "artifacts": str(case.context.artifacts_dir), "command": sys.executable, "args": [str(script), *ARGUMENTS],
                            "cwd": str(working), "receipts": str(receipts), "endpoint": endpoint.url,
                            "envSecret": ENV_SECRET, "headerSecret": HEADER_SECRET})})
            (case.context.artifacts_dir / "browser.stdout.raw").write_bytes(result.stdout)
            (case.context.artifacts_dir / "browser.stderr.raw").write_bytes(result.stderr)
            (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command,
                "exit_code": result.returncode, "memory_limit_kb": 4194304, "timeout_seconds": 180}, indent=2), encoding="utf-8")
            assert result.returncode == 0, (result.stdout + result.stderr).decode("utf-8", errors="backslashreplace")
            saved = commentjson.loads((case.workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))["mcp"]["servers"]
            assert saved["manual-stdio"]["args"] == [str(script), *ARGUMENTS]
            assert saved["manual-stdio"]["cwd"] == str(working)
            assert saved["manual-stdio"]["env"]["MCP_PRIVATE_TOKEN"]["kind"] == "mcp_private_env"
            assert saved["manual-http"]["headers"]["X-Private-Token"]["kind"] == "mcp_private_header"
            assert saved["manual-http"]["url"] == endpoint.url
            assert all("managed_launch" not in saved[server] and saved[server]["enabled"] for server in ("manual-stdio", "manual-http"))

            def invoke() -> dict[str, object]:
                session_response = case.client.post("/api/v1/sessions", json={"title": "手动 MCP 实际目标调用"})
                session_response.raise_for_status()
                session = session_response.json()["data"]
                tools = [("manual-stdio-call", "mcp__manual-stdio__private_env_probe", {"proof": "manual-ui", "include_inputs": True}),
                    ("manual-http-call", "mcp__manual-http__probe", {"proof": "manual-http-ui"})]
                replies = [{"tool_calls": [{"index": index, "id": call_id, "type": "function", "function": {
                    "name": "invoke_extension_tool", "arguments": json.dumps({"tool_name": name, "arguments": arguments})}}
                    for index, (call_id, name, arguments) in enumerate(tools)]}, {"content": "已验证手动 MCP。"}]
                calls = case.replay.control.plan_model_calls(session["session_id"], session["thread_id"], [
                    cast(list[dict[str, object]], build_cassette(asset_id=f"manual-mcp-{ordinal}", model="e2e-stub-model",
                        url="https://opencode.ai/zen/v1/chat/completions", frames=chat_completion_frames(response_id=f"manual-mcp-{ordinal}",
                            model="e2e-stub-model", deltas=({"role": "assistant", **reply},),
                            finish_reason="tool_calls" if "tool_calls" in reply else "stop")).raw["interactions"])[0]
                    for ordinal, reply in enumerate(replies)])
                accepted = case.client.post(f"/api/v1/sessions/{session['session_id']}/messages", json={
                    "message": {"content": "调用手动 stdio 和 HTTP 探针，核对实际目标。"}, "run": {"mode": "single_agent", "agent_id": "default"}})
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
                messages = {message["tool_call_id"]: message for message in record["request"]["messages"] if message.get("role") == "tool"}
                assert set(messages) == {"manual-stdio-call", "manual-http-call"}
                for message in messages.values():
                    assert message["name"] == "invoke_extension_tool"
                stdio = json.loads(messages["manual-stdio-call"]["content"][0]["text"])
                assert stdio == {"proof": "manual-ui", "secret_digest": hashlib.sha256(ENV_SECRET.encode()).hexdigest(),
                    "prefix": sys.prefix, "arguments": ARGUMENTS,
                    "environment": {"MCP_PLAIN_VALUE": "ordinary", "MCP_EMPTY_VALUE": "", "MCP_SKIPPED_VALUE": None}}
                assert json.loads(messages["manual-http-call"]["content"][0]["text"]) == {"name": "probe", "arguments": {"proof": "manual-http-ui"}}
                _, base = installation_route(case.client)
                inventory = case.client.get(base + "/installations")
                inventory.raise_for_status()
                assert inventory.json()["data"] == []
                return {"session_id": session["session_id"], "thread_id": session["thread_id"], "model_call_ids": calls, "messages": messages}

            evidence = await asyncio.to_thread(invoke)
            assert observations == [{"name": "probe", "arguments": {"proof": "manual-http-ui"},
                "private_header_matches": True, "plain_header_matches": True}]
            assert any(json.loads(line).get("proof") == "manual-ui" for line in receipts.read_text(encoding="utf-8").splitlines())
            (case.context.artifacts_dir / "manual-thread-result.json").write_text(json.dumps({
                **evidence, "http_observations": observations}, indent=2), encoding="utf-8")
            private = case.workspace / ".boxteam/credentials"
            for directory in (case.context.run_root, case.workspace):
                for path in directory.rglob("*"):
                    if path.is_file() and not path.is_relative_to(private):
                        content = path.read_bytes()
                        assert ENV_SECRET.encode() not in content and HEADER_SECRET.encode() not in content, str(path)
        finally:
            await asyncio.to_thread(_disconnect_http, case)
