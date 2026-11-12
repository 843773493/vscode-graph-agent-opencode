"""真实 Workbench 在显式重连远程 Gateway 后恢复原插件命令订阅。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

import commentjson
import pytest

from app.gateway.federation.ports import principal_ref_for_gateway
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import (
    TargetGatewayNetwork,
)
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_network as target_gateway_network,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_port as target_gateway_port,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
from tests.support.plugin_package import install_plugin, native_plugin_target


@pytest.fixture(autouse=True)
def workbench_gateway_environment(
    monkeypatch: pytest.MonkeyPatch, web_workbench_assets: Path,
) -> None:
    monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))


def test_remote_command_subscriber_recovers_after_explicit_reconnect(
    target_gateway_network: TargetGatewayNetwork,
) -> None:
    network = target_gateway_network
    context = network.context
    client_a, client_b = network.clients
    gateway_a, gateway_b = network.gateway_ids
    output = context.artifacts_dir / "remote-command-executions.jsonl"
    package_path = context.artifacts_dir / "remote-command-plugin.zip"
    manifest = {
        "manifest_version": 1,
        "package_id": "proof.remote-command-ui",
        "version": "1.0.0",
        "name": "远程命令重连验收包",
        "extension_contract_version": 1,
        "target": native_plugin_target("python"),
        "plugins": [{
            "id": "commands",
            "name": "远程命令",
            "instance_scope": "gateway",
            "runtime": "python",
            "entrypoint": ["entry.py"],
            "management_protocol_version": 1,
            "requires": [
                {"extension_point": "commands", "version": 1},
                {"extension_point": "configuration", "version": 1},
            ],
            "contributes": {
                "commands": [{
                    "id": "execute",
                    "handler": "execute",
                    "title": "远程命令验收：执行",
                    "arguments_schema": {"type": "object", "additionalProperties": False,
                        "properties": {"value": {"type": "string"}}},
                    "result_schema": {"type": "object", "required": ["value", "pid", "count"],
                        "properties": {"value": {"type": "string"}, "pid": {"type": "integer"},
                            "count": {"type": "integer"}}},
                    "required_capabilities": ["read"],
                }],
                "configuration": {"type": "object", "required": ["output"],
                    "properties": {"output": {"type": "string"}}},
            },
        }],
    }
    plugin_entry = '''
import sys, os, asyncio, json
sys.path.insert(0, os.environ['BOXTEAM_PROJECT_ROOT'])
from fastapi import FastAPI
import uvicorn
from app.core.logging_config import configure_application_logging
from app.core.trace_middleware import TraceMiddleware
from app.runtime.plugin_commands import plugin_command_router
configure_application_logging(service='remote-command-reconnect-proof')
calls = 0
async def execute(arguments, context):
    global calls
    calls += 1
    result = {'call_id': context.call.call_id, 'value': arguments.get('value', 'palette'),
        'pid': os.getpid(), 'count': calls}
    with open(context.parameters['output'], 'a', encoding='utf-8') as output:
        output.write(json.dumps(result) + '\\n')
    return {key: value for key, value in result.items() if key != 'call_id'}
async def shutdown(): server.should_exit = True
app = FastAPI()
app.add_middleware(TraceMiddleware)
app.include_router(plugin_command_router({'execute': execute}, shutdown=shutdown))
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(os.environ['BOXTEAM_PLUGIN_PORT']), log_config=None))
asyncio.run(server.serve())
'''
    with zipfile.ZipFile(package_path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("entry.py", plugin_entry)
    install_plugin(
        lambda path, payload: client_b.post(path, json=payload),
        gateway_id=gateway_b,
        path=str(package_path.resolve()),
    )

    def edit_remote_gateway(update: Callable[[dict[str, object]], None]) -> dict:
        path = "/api/gateway/config/sources/gateway_mutable_override"
        source = client_b.get(path).raise_for_status().json()["data"]
        document = commentjson.loads(source["jsonc"])
        update(document)
        saved = client_b.put(path, json={
            "gateway_id": gateway_b,
            "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(document, ensure_ascii=False),
            "base_file_digest": source["file_digest"],
            "base_layer_revision": source["layer_revision"],
            "base_layer_digest": source["layer_digest"],
            "expected_active_revision": source["active_revision"],
            "expected_active_digest": source["active_digest"],
            "idempotency_key": "remote-command-reconnect-config",
        }).raise_for_status().json()["data"]
        assert saved["edit"]["state"] == "active", saved
        return saved

    def bind_plugin(document: dict[str, object]) -> None:
        plugins = document.setdefault("plugins", {})
        assert isinstance(plugins, dict)
        plugins["runtime"] = {"entries": [{
            "package_id": "proof.remote-command-ui", "plugin_id": "commands",
            "package_version": "1.0.0", "enabled": True,
        }]}
        plugins["configuration"] = {"values": {"proof.remote-command-ui": {
            "commands": {"output": str(output.resolve())},
        }}}
        principal_ref = principal_ref_for_gateway(gateway_a)
        document["permissions"] = {"federation": {"default_effect": "deny", "rules": [
            {"capability": capability, "effect": "allow", "principal_ref": principal_ref}
            for capability in ("read", "send")
        ]}}

    edit_remote_gateway(bind_plugin)
    deadline = time.monotonic() + 25
    initial_status = None
    while time.monotonic() < deadline:
        initial_status = client_b.post("/api/gateway/service-plugins/status", json={
            "gateway_id": gateway_b,
        }).raise_for_status().json()["data"]
        plugin = next(item for item in initial_status["plugins"]
            if item["binding"]["package_id"] == "proof.remote-command-ui")
        if plugin["state"] == "ready":
            break
        time.sleep(0.1)
    assert plugin["state"] == "ready" and plugin["instance"] is not None, plugin
    initial_instance = plugin["instance"]

    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    connection = client_a.post("/api/gateway/remote-gateways", json={
        "name": "命令重连远端 B", "host": "127.0.0.1", "port": 22,
        "username": "command-reconnect", "private_key_path": topology["private_key"],
        "remote_gateway_port": topology["ports"][3],
    }).raise_for_status().json()["data"]
    projected, = [item for item in connection["items"] if item["gateway_id"] == gateway_b]
    remote_connection, = projected["remote_gateways"]
    connection_id = remote_connection["gateway_connection_id"]
    (network.workspace_roots[1] / "remote-command-owner.txt").write_text("远程命令归属\n", encoding="utf-8")

    result_path = context.artifacts_dir / "remote-command-reconnect.json"
    screenshot_path = context.artifacts_dir / "remote-command-reconnect-failure.png"
    script = "tests/integration/clients/web/plugin_command_remote_reconnect.mjs"
    node = shutil.which("node")
    assert node, "Workbench Playwright 驱动需要发行 Node"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if os.name == "nt" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "180",
        "--", node, script]
    with chromium_temporary_environment(context.runtime_root_for_node("remote-command-browser") / "chromium-tmp") as temporary:
        completed = subprocess.run(command, cwd=context.project_root, timeout=195, check=False,
            capture_output=True, env={**os.environ, **temporary,
                "BOXTEAM_REMOTE_PLUGIN_COMMAND_INPUT": json.dumps({
                    "baseUrl": str(client_a.base_url), "gatewayA": gateway_a, "gatewayB": gateway_b,
                    "workspaceId": network.workspace_id, "connectionId": connection_id,
                    "initialInstanceId": initial_instance["instance_id"],
                    "initialProcessId": initial_instance["process_id"],
                    "output": str(output.resolve()), "result": str(result_path),
                    "screenshot": str(screenshot_path),
                })})
    (context.artifacts_dir / "remote-command-browser.stdout.raw").write_bytes(completed.stdout)
    (context.artifacts_dir / "remote-command-browser.stderr.raw").write_bytes(completed.stderr)
    (context.artifacts_dir / "remote-command-browser-process.json").write_text(json.dumps({
        "command": command, "exit_code": completed.returncode,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    assert completed.returncode == 0, f"{stdout}\n{stderr}\n{result_path}"
    receipt = json.loads(result_path.read_text(encoding="utf-8"))
    reconnected_status = client_b.post("/api/gateway/service-plugins/status", json={
        "gateway_id": gateway_b,
    }).raise_for_status().json()["data"]
    reconnected_plugin = next(item for item in reconnected_status["plugins"]
        if item["binding"]["package_id"] == "proof.remote-command-ui")
    assert reconnected_plugin["state"] == "ready", reconnected_plugin
    reconnected_instance = reconnected_plugin["instance"]
    assert reconnected_instance["instance_id"] == initial_instance["instance_id"]
    assert reconnected_instance["process_id"] == initial_instance["process_id"]
    assert receipt["phase"] == "complete" and receipt["pageErrors"] == [], receipt
    assert receipt["initialInstanceId"] == receipt["reconnectedInstanceId"] == initial_instance["instance_id"]
    executions = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(executions) == 2 and len({item["call_id"] for item in executions}) == 2, receipt
    assert [item["count"] for item in executions] == [1, 2], receipt
    assert receipt["subscriptionScopes"] == [None, network.workspace_id]
    assert all(item["request_count"] == 2 and item["old_stream_closed"] for item in receipt["subscriptionReceipts"])
    assert len(receipt["calls"]) == 2
    assert all(call["gateway_id"] == gateway_b and call["gateway_connection_id"] == connection_id for call in receipt["calls"])
    assert receipt["executionsBeforeSecondCall"] == 1
