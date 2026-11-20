"""只用完整包内依赖，验证独立 Python SDK 和真实 MCP bridge。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import TextIO

import httpx
import pytest
from boxteam_plugin_sdk.logging import runtime_log_file_identity
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.harness.python.run_context import TestRunContext
from tests.support.processes.workspace import terminate_process


@pytest.fixture
def offline_runtime(request: pytest.FixtureRequest, prepared_mcp_stdio_package: Path,
                    unused_tcp_port: int) -> tuple[Path, dict[str, str], TestRunContext, int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = context.run_root / "runtime" / request.node.name
    root.mkdir(parents=True)
    with zipfile.ZipFile(prepared_mcp_stdio_package) as archive:
        archive.extractall(root / "package")
    dependencies = root / "package/python/site-packages"
    # -S 排除全局与开发 venv，唯一附加路径必须来自制品。审计拒绝外网和产品导入。
    guard = root / "guard.py"
    guard.write_text('''import ipaddress, runpy, sys
def audit(event, args):
    if event == "import" and args[0].split(".")[0] in {"app", "configs"}:
        raise RuntimeError("离线制品不得导入产品源码")
    if event == "socket.connect":
        address = args[1]
        if not isinstance(address, tuple) or not ipaddress.ip_address(address[0]).is_loopback:
            raise RuntimeError("离线制品不得连接外网")
sys.addaudithook(audit)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
''', encoding="utf-8")
    environment = {key: value for key, value in os.environ.items()
        if not key.startswith(("BOXTEAM_", "PYTHON", "UV_", "PIP_"))}
    environment.update({"PYTHONPATH": str(dependencies), "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "BOXTEAM_RUNTIME_CONTROL_TOKEN": "offline-control",
        "BOXTEAM_PLUGIN_PORT": str(unused_tcp_port),
        "BOXTEAM_PLUGIN_INSTANCE": json.dumps({"gateway_id": "offline-gateway", "package_id": "offline.probe",
            "package_version": "1.0.0", "plugin_id": "probe", "instance_scope": "workspace",
            "workspace_id": "offline-workspace", "instance_id": "offline-instance",
            "management_protocol_version": 1, "extension_contract_version": 1, "services": []}),
        "BOXTEAM_PLUGIN_COMMANDS": json.dumps([{"id": "inspect", "title": "Inspect", "handler": "inspect",
            "arguments_schema": {"type": "object"}, "result_schema": {"type": "object"}}]),
        "BOXTEAM_RUNTIME_LOGGING": json.dumps({"revision": "initial", "level": "info", "pretty": False}),
        "BOXTEAM_RUNTIME_OBSERVATION": json.dumps({"instance_id": "offline-instance", "revision": "initial",
            "telemetry": {"enabled": False}, "statistics": {"enabled": False}})})
    return root, environment, context, unused_tcp_port


def _start(root: Path, env: dict[str, str], script: Path, log: Path) -> tuple[subprocess.Popen, TextIO]:
    output = log.open("w", encoding="utf-8")
    device, inode = runtime_log_file_identity(output.fileno())
    env = {**env, "BOXTEAM_RUNTIME_LOG_SINK": json.dumps({"path": str(log), "owner_id": "offline-instance",
        "role": "service-plugin", "source": "offline-test", "device": device, "inode": inode})}
    try:
        process = subprocess.Popen([sys.executable, "-S", str(root / "guard.py"), str(script)],
            cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT, text=True)
    except BaseException:
        output.close()
        raise
    return process, output


async def _ready(client: httpx.AsyncClient, process: subprocess.Popen) -> dict:
    deadline = time.monotonic() + 15
    while True:
        assert process.poll() is None, f"离线插件提前退出: {process.returncode}"
        try:
            response = await client.get("/internal/plugin/status")
        except httpx.ConnectError:
            assert time.monotonic() < deadline, "离线插件未就绪"
            await asyncio.sleep(0.02)
            continue
        response.raise_for_status()
        return response.json()


async def test_independent_sdk_maintenance_commands_logging_and_drain(offline_runtime) -> None:
    root, environment, context, port = offline_runtime
    script = root / "plugin.py"
    shutil.copyfile(Path(__file__).with_name("maintenance_plugin.py"), script)
    environment["MAINTENANCE_RECEIPT"] = str(root / "maintenance.json")
    log = context.artifacts_dir / "independent-sdk.log"
    process, output = _start(root, environment, script, log)
    evidence = {}
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"X-Local-Token": "offline-control", "X-Request-ID": "offline-sdk-request"}, timeout=5) as client:
            status = await _ready(client, process)
            assert status["instance_id"] == "offline-instance"
            registrations = (await client.get("/internal/plugin/commands")).raise_for_status().json()
            registration, = registrations["commands"]
            initial = (await client.get("/internal/runtime-logging")).raise_for_status().json()
            logging_state = initial["data"]
            for revision, level in (("debug-enabled", "debug"), ("warning-only", "warning")):
                applied = (await client.post("/internal/runtime-logging", json={
                    "runtime_instance_id": logging_state["runtime_instance_id"],
                    "revision": revision, "expected_revision": logging_state["applied_revision"],
                    "level": level, "pretty": False})).raise_for_status().json()
                assert applied["request_id"] == "offline-sdk-request"
                logging_state = applied["data"]
                assert logging_state["applied_revision"] == revision and logging_state["level"] == level
                response = (await client.post("/internal/plugin/commands/call", json={
                    "gateway_id": "offline-gateway", "package_id": "offline.probe", "package_version": "1.0.0",
                    "plugin_id": "probe", "workspace_id": "offline-workspace", "instance_id": "offline-instance",
                    "registration_id": registration["registration_id"], "command_id": "inspect",
                    "call_id": revision, "remaining_ms": 5000, "arguments": {}})).raise_for_status()
                events = [json.loads(line) for line in response.text.splitlines()]
                assert [event["kind"] for event in events] == ["started", "result"]
                assert all(event["request_id"] == "offline-sdk-request" for event in events)
                result = events[-1]["result"]
                assert result["count"] > 0 and result["cwd"] == str(root)
                assert Path(result["sdk"]).is_relative_to(root / "package/python/site-packages")
                assert result["contract"] == json.loads((Path.cwd() / "resources/logging/runtime-contract.json").read_text())
                evidence[revision] = {"logging": applied, "events": events}
            receipt = root / "maintenance.json"
            first = json.loads(receipt.read_text())["count"]
            deadline = time.monotonic() + 3
            while json.loads(receipt.read_text())["count"] <= first:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
            accepted = (await client.post("/internal/plugin/shutdown")).raise_for_status().json()
            assert accepted["accepted"] is True
        assert await asyncio.to_thread(process.wait, 10) == 0
        final_count = json.loads(receipt.read_text())["count"]
        assert final_count > first
        output.flush()
        text = log.read_text()
        assert text.count("SDK_DEBUG_MARKER") == 1 and text.count("SDK_WARNING_MARKER") == 2
        assert f"MAINTENANCE_DRAINED count={final_count}" in text and 'request_id="offline-sdk-request"' in text
    finally:
        try:
            terminate_process(process)
        finally:
            output.close()
            (context.artifacts_dir / "independent-sdk-evidence.json").write_text(json.dumps(evidence, indent=2))


async def test_complete_mcp_bridge_uses_bundled_sdk_without_product_imports(offline_runtime) -> None:
    root, environment, context, port = offline_runtime
    server = root / "schema_server.py"
    shutil.copyfile(Path.cwd() / "tests/integration/workspace_services/mcp/schema_server.py", server)
    definitions, receipts = root / "definitions.json", root / "receipts.jsonl"
    definitions.write_text(json.dumps({"probe": {"type": "object"}}))
    environment["BOXTEAM_PLUGIN_PARAMETERS"] = json.dumps({"command": sys.executable,
        "args": ["-S", str(root / "guard.py"), str(server), str(definitions), str(receipts)], "cwd": str(root),
        "env": {"PYTHONPATH": environment["PYTHONPATH"], "PYTHONNOUSERSITE": "1"}})
    process, output = _start(root, environment, root / "package/bridge.py", context.artifacts_dir / "mcp-bridge.log")
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
            headers={"X-Local-Token": "offline-control", "X-Request-ID": "offline-mcp-request"}, timeout=5) as client:
            status = await _ready(client, process)
            assert status["process_id"] == process.pid
            async with (
                streamable_http_client(f"http://127.0.0.1:{port}/services/mcp/", http_client=client) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == ["probe"]
                result = await session.call_tool("probe", {"offline": True})
                assert not result.isError
                assert json.loads(result.content[0].text) == {"name": "probe", "arguments": {"offline": True}}
            assert json.loads(receipts.read_text()) == {"name": "probe", "arguments": {"offline": True}}
            accepted = (await client.post("/internal/plugin/shutdown")).raise_for_status().json()
            assert accepted["accepted"] is True
        assert await asyncio.to_thread(process.wait, 10) == 0
    finally:
        try:
            terminate_process(process)
        finally:
            output.close()
