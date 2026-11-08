"""真实 Gateway／Python／Node 插件验证具名注册、流进度、取消与实例失效。"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import install_test_workspace_config

PYTHON = '''
import sys, os, asyncio
sys.path.insert(0, os.environ['BOXTEAM_PROJECT_ROOT'])
from fastapi import FastAPI
import uvicorn
from app.core.logging_config import configure_application_logging
from app.core.trace_middleware import TraceMiddleware
from app.runtime.plugin_commands import plugin_command_router
configure_application_logging(service='command-proof')
async def execute(arguments, context):
    await context.progress(1, 2, context.request_id + ':' + context.call.call_id)
    if arguments['mode'] == 'wait': await asyncio.Event().wait()
    if arguments['mode'] == 'error': raise ValueError('command-proof-error')
    if arguments['mode'] == 'bad-result': return {'wrong': True}
    if arguments['mode'] == 'null': return None
    return {'runtime': 'python', 'pid': os.getpid(), 'value': arguments['value']}
async def shutdown(): server.should_exit = True
app = FastAPI()
app.add_middleware(TraceMiddleware)
app.include_router(plugin_command_router({'execute': execute}, shutdown=shutdown))
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(os.environ['BOXTEAM_PLUGIN_PORT']),
    log_config=None))
asyncio.run(server.serve())
'''

NODE = '''
import http from 'node:http';
import { createPluginRuntimeManagement } from './src/shared/serverPluginRuntime.mjs';
import { createRuntimeLogger } from './src/shared/serverRuntimeLogging.mjs';
const logger = createRuntimeLogger('command-proof');
const readJson = async req => { let text=''; for await (const part of req) text+=part; return text ? JSON.parse(text) : {}; };
const management = createPluginRuntimeManagement({service:'command-proof', logger, readJson,
  shutdown: () => new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve())),
  commands: { execute: async (args, {signal, progress, requestId, call}) => {
    await progress(1, 2, requestId + ':' + call.call_id);
    if (args.mode === 'wait') await new Promise((resolve, reject) => {
      signal.throwIfAborted(); signal.addEventListener('abort', () => reject(signal.reason), {once:true});
    });
    if (args.mode === 'error') throw new Error('command-proof-error');
    if (args.mode === 'bad-result') return {wrong:true};
    if (args.mode === 'null') return null;
    return {runtime:'node', pid:process.pid, value:args.value};
  }} });
const server = http.createServer(async(req,res) => {
  try { if (!await management.handleRequest(req,res,new URL(req.url,'http://localhost').pathname)) {
    res.writeHead(404);res.end();
  }} catch(error) { logger.error('request failed',error); res.writeHead(error.statusCode ?? 500);res.end(JSON.stringify({detail:error.message})); }
});
server.listen(management.port, '127.0.0.1');
'''


@pytest.fixture
def plugin_gateway(request: pytest.FixtureRequest, integration_workspace_root_path: str,
                   unused_tcp_port: int) -> Iterator[tuple[httpx.Client, TestRunContext]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    config_path = install_test_workspace_config(workspace_root=workspace,
        config_path=Path("configs/tests/workspace/default.jsonc"), schema_path=Path("configs/workspace_schema.jsonc"))
    config = commentjson.loads(config_path.read_text())
    for provider in config["llm"]["providers"]:
        if "api_key" in provider:
            provider["api_key"] = "command-test-unused-key"
    config_path.write_text(json.dumps(config))
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None, port=unused_tcp_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
                          timeout=30, trust_env=False) as client:
            yield client, context
    finally:
        close_gateway_process(gateway)


def _archive(destination: Path) -> None:
    command = {"id": "execute", "title": "Execute", "handler": "execute", "required_capabilities": ["read"],
        "arguments_schema": {"type": "object", "additionalProperties": False, "required": ["mode", "value"],
            "properties": {"mode": {"enum": ["return", "wait", "error", "bad-result", "null"]}, "value": {"type": "string"}}},
        "result_schema": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False, "required": ["runtime", "pid", "value"],
            "properties": {"runtime": {"enum": ["python", "node"]}, "pid": {"type": "integer"}, "value": {"type": "string"}}}
        ]}}
    manifest = {"package_id": "proof.commands", "version": "1.0.0", "name": "Command proof", "plugins": [
        {"id": runtime, "name": runtime, "instance_scope": "gateway", "runtime": runtime,
         "entrypoint": ["entry.py" if runtime == "python" else "entry.mjs"], "management_protocol_version": 1,
         "requires": [{"extension_point": "commands", "version": 1}], "contributes": {"commands": [command]}}
        for runtime in ("python", "node")]}
    package = json.loads(Path("resources/plugins/developer-tools/package.json").read_text())
    package["name"] = "command-proof"
    package["dependencies"] = {key: value for key, value in package["dependencies"].items()
                               if key in {"@bufbuild/protobuf", "ajv"}}
    lock = json.loads(Path("resources/plugins/developer-tools/package-lock.json").read_text())
    lock["name"] = package["name"]
    lock["packages"][""] = {"name": package["name"], "version": package["version"], "dependencies": package["dependencies"]}
    for key in list(lock["packages"]):
        if key in {f"node_modules/{name}" for name in ("node-pty", "node-addon-api", "playwright", "playwright-core", "fsevents", "ws")}:
            del lock["packages"][key]
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("entry.py", PYTHON)
        archive.writestr("entry.mjs", NODE)
        archive.writestr("package.json", json.dumps(package))
        archive.writestr("package-lock.json", json.dumps(lock))
        for file in ("serverPluginRuntime.mjs", "serverPluginCommands.mjs", "serverRuntimeControl.mjs", "serverRuntimeLogging.mjs"):
            archive.write(Path("src/shared") / file, f"src/shared/{file}")
        for file in ("plugin_commands_pb.js", "service_plugin_pb.js"):
            source = Path("src/workspace-services/protocol/generated/boxteam/gateway/v2") / file
            archive.write(source, source.as_posix())


def test_registered_commands_progress_cancel_restart_and_disabled_admission(
    plugin_gateway: tuple[httpx.Client, TestRunContext],
) -> None:
    client, context = plugin_gateway
    client.post("/api/gateway/users/guest", json={"tracking": {"test": True}}).raise_for_status()
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    package = context.artifacts_dir / "commands.zip"
    _archive(package)
    client.post("/api/gateway/service-plugins/install", json={"gateway_id": gateway_id,
        "path": str(package.resolve())}).raise_for_status()
    bindings = [{"package_id": "proof.commands", "plugin_id": runtime, "package_version": "1.0.0", "enabled": True}
                for runtime in ("python", "node")]

    def bind(enabled: bool) -> dict[str, dict]:
        source_path = "/api/gateway/config/sources/gateway_mutable_override"
        document = client.get(source_path).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source.setdefault("plugins", {})["runtime"] = {"entries": [{**item, "enabled": enabled} for item in bindings]}
        client.put(source_path, json={"gateway_id": gateway_id, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(source), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": f"commands-{enabled}"}).raise_for_status()
        return {item["binding"]["plugin_id"]: item for item in client.post("/api/gateway/service-plugins/status",
            json={"gateway_id": gateway_id}).raise_for_status().json()["data"]["plugins"]}

    states = bind(True)
    assert set(states) == {"python", "node"}
    evidence = []
    last_call: dict[str, object] = {}
    for runtime, state in states.items():
        assert state["state"] == "ready", state
        instance = {"gateway_id": gateway_id, "package_id": "proof.commands", "package_version": "1.0.0",
            "plugin_id": runtime, "instance_id": state["instance"]["instance_id"]}
        registration = client.post("/api/gateway/service-plugins/commands/registrations",
            json=instance).raise_for_status().json()["data"]["commands"][0]
        identity = {**instance, "registration_id": registration["registration_id"], "command_id": "execute"}
        for mode, terminal in (("return", "result"), ("null", "result"), ("wait", "cancelled"), ("error", "error"), ("bad-result", "error")):
            call = {**identity, "call_id": f"{runtime}-{mode}", "remaining_ms": 5000,
                    "arguments": {"mode": mode, "value": "命令结果 Ω"}}
            last_call = call
            with client.stream("POST", "/api/gateway/service-plugins/commands/call", json=call,
                headers={"X-Request-ID": f"request-{runtime}-{mode}"}) as response:
                response.raise_for_status()
                events = []
                for line in response.iter_lines():
                    event = json.loads(line)
                    events.append(event)
                    if mode == "wait" and event["kind"] == "progress":
                        cancelled = client.post("/api/gateway/service-plugins/commands/cancel",
                            json={**identity, "call_id": call["call_id"]}).raise_for_status().json()["data"]
                        assert cancelled["accepted"] is True
                assert [event["kind"] for event in events] == ["started", "progress", terminal], events
                assert [event["sequence"] for event in events] == [1, 2, 3]
                assert events[1]["message"] == f"request-{runtime}-{mode}:{call['call_id']}"
                for event in events:
                    for field in ("instance_id", "registration_id", "command_id", "call_id"):
                        assert event[field] == call[field]
                    assert event["request_id"] == f"request-{runtime}-{mode}"
                if mode == "return":
                    assert events[-1]["result"] == {"runtime": runtime, "pid": state["instance"]["process_id"], "value": "命令结果 Ω"}
                elif mode == "null":
                    assert events[-1]["result"] is None
                evidence.extend(events)
        try:
            timed = client.post("/api/gateway/service-plugins/commands/call", json={**identity,
                "call_id": f"{runtime}-deadline", "remaining_ms": 150,
                "arguments": {"mode": "wait", "value": "期限"}})
        except httpx.RemoteProtocolError:
            # 总期限先于终态送达时断开原流，不能伪造命令结果。
            pass
        else:
            timed.raise_for_status()
            timed_events = [json.loads(line) for line in timed.iter_lines()]
            assert timed_events[-1]["kind"] == "error"
            assert timed_events[-1]["error_code"] == "deadline-exceeded"
        following = client.post("/api/gateway/service-plugins/commands/call", json={**identity,
            "call_id": f"{runtime}-after-deadline", "remaining_ms": 5000,
            "arguments": {"mode": "return", "value": "期限后原实例仍可用"}}).raise_for_status()
        assert json.loads(list(following.iter_lines())[-1])["kind"] == "result"
        invalid = {**call, "call_id": f"{runtime}-invalid", "arguments": {"mode": "return"}}
        assert client.post("/api/gateway/service-plugins/commands/call", json=invalid).status_code == 422
        assert client.post("/api/gateway/service-plugins/commands/call", json={**call,
            "registration_id": "stale"}).status_code == 409
        # 重启合同不接收实例字段；配置 owner 的 revision 是唯一期望来源。
        restarted = client.post("/api/gateway/service-plugins/restart", json={key: value for key, value in
            {**instance, "revision": state["revision"]}.items() if key != "instance_id"})
        assert restarted.raise_for_status().json()["data"]["instance"]["instance_id"] != instance["instance_id"]
        assert client.post("/api/gateway/service-plugins/commands/call", json=call).status_code == 409
    disabled = bind(False)
    assert all(state["state"] == "stopped" for state in disabled.values())
    assert client.post("/api/gateway/service-plugins/commands/call", json=last_call).status_code == 409
    (context.artifacts_dir / "command-events.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
