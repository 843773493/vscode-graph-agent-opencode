"""真实 Gateway／Python／Node 插件验证具名注册、流进度、取消与实例失效。"""

from __future__ import annotations

import json
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import commentjson
import httpx
import psutil
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.plugin_package import install_plugin, native_plugin_target
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)

SnapshotReader = Callable[[Callable[[dict], bool]], dict]

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
    if arguments['mode'] == 'configured': return {'runtime': 'python', 'pid': os.getpid(), 'value': context.parameters['prefix'] + arguments['value']}
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
  commands: { execute: async (args, {signal, progress, requestId, call, parameters}) => {
    await progress(1, 2, requestId + ':' + call.call_id);
    if (args.mode === 'wait') await new Promise((resolve, reject) => {
      signal.throwIfAborted(); signal.addEventListener('abort', () => reject(signal.reason), {once:true});
    });
    if (args.mode === 'error') throw new Error('command-proof-error');
    if (args.mode === 'bad-result') return {wrong:true};
    if (args.mode === 'null') return null;
    if (args.mode === 'configured') return {runtime:'node', pid:process.pid, value:parameters.prefix + args.value};
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


@pytest.fixture
def command_snapshots(plugin_gateway: tuple[httpx.Client, TestRunContext]) -> Iterator[SnapshotReader]:
    client, context = plugin_gateway
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    evidence: list[dict] = []
    with client.stream("POST", "/api/gateway/service-plugins/commands/events", json={"gateway_id": gateway_id},
                       headers={"X-Request-ID": "command-publications"}) as response:
        response.raise_for_status()
        assert response.headers["X-Request-ID"] == "command-publications"
        lines = response.iter_lines()

        def read(matches: Callable[[dict], bool]) -> dict:
            deadline = time.monotonic() + 20
            for line in lines:
                assert time.monotonic() < deadline, "原命令绑定没有发布预期快照"
                if line.startswith("data: "):
                    snapshot = json.loads(line[6:])
                    assert snapshot["gateway_id"] == gateway_id and snapshot["workspace_id"] is None
                    evidence.append(snapshot)
                    if matches(snapshot):
                        return snapshot
            raise AssertionError("原贡献订阅在预期状态之前结束")

        assert read(lambda _: True)["publications"] == []
        try:
            yield read
        finally:
            (context.artifacts_dir / "command-publications.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))


def _archive(destination: Path, prepared_package_root: Path, *, python_source: str = PYTHON,
             version: str = "1.0.0", scope: str = "gateway") -> None:
    command = {"id": "execute", "title": "Execute", "handler": "execute", "required_capabilities": ["read"],
        "arguments_schema": {"type": "object", "additionalProperties": False, "required": ["mode", "value"],
            "properties": {"mode": {"enum": ["return", "wait", "error", "bad-result", "null", "configured"]}, "value": {"type": "string"}}},
        "result_schema": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False, "required": ["runtime", "pid", "value"],
            "properties": {"runtime": {"enum": ["python", "node"]}, "pid": {"type": "integer"}, "value": {"type": "string"}}}
        ]}}
    manifest = {"package_id": "proof.commands", "version": version, "name": "Command proof",
        "target": native_plugin_target("python", "node"), "plugins": [
        {"id": runtime, "name": runtime, "instance_scope": scope, "runtime": runtime,
         "entrypoint": ["entry.py" if runtime == "python" else "entry.mjs"], "management_protocol_version": 1,
         "requires": [{"extension_point": "commands", "version": 1}, {"extension_point": "configuration", "version": 1}],
         "contributes": {"commands": [command], "configuration": {"type": "object", "additionalProperties": False,
             "properties": {"prefix": {"type": "string"}, "credentialValue": {"type": "string", "writeOnly": True}}}}}
        for runtime in ("python", "node")]}
    package = json.loads((prepared_package_root / "package.json").read_text())
    package["name"] = "command-proof"
    lock = commentjson.loads((prepared_package_root / "bun.lock").read_text())
    lock["workspaces"][""] = {"name": package["name"], "dependencies": package["dependencies"]}
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("entry.py", python_source)
        archive.writestr("entry.mjs", NODE)
        archive.writestr("package.json", json.dumps(package))
        archive.writestr("bun.lock", json.dumps(lock))
        for file in sorted((prepared_package_root / "node_modules").rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(prepared_package_root).as_posix())
        for file in ("serverPluginRuntime.mjs", "serverPluginCommands.mjs", "serverRuntimeControl.mjs", "serverRuntimeLogging.mjs", "serverRuntimeObservation.mjs"):
            archive.write(Path("src/shared") / file, f"src/shared/{file}")
        for file in ("plugin_commands_pb.js", "service_plugin_pb.js", "public_pb.js"):
            source = Path("src/workspace-services/protocol/generated/boxteam/gateway/v2") / file
            archive.write(source, source.as_posix())


def test_workspace_commands_release_removed_target_without_changing_sibling(
    plugin_gateway: tuple[httpx.Client, TestRunContext],
    prepared_developer_tools_package: Path,
    request: pytest.FixtureRequest,
) -> None:
    client, context = plugin_gateway
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    evidence: list[dict] = []
    targets: list[dict[str, str]] = []

    def exchange(method: str, path: str, body: dict | None = None) -> httpx.Response:
        request_id = f"workspace-command-{len(evidence)}"
        response = client.request(method, path, json=body, headers={"X-Request-ID": request_id}, timeout=75)
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"] == request_id
        evidence.append({"path": path, "status": response.status_code, "body": payload})
        return response

    def bind(target: dict[str, str], version: str | None) -> None:
        path = f"/api/v1/workspaces/{target['workspace_id']}/config/sources/workspace_root_mutable_override"
        document = exchange("GET", path).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source["plugins"] = {
            "runtime": {"entries": [] if version is None else [
                {"package_id": "proof.commands", "plugin_id": runtime, "package_version": version, "enabled": True}
                for runtime in ("python", "node")
            ]},
            "configuration": {"values": {"proof.commands": {
                runtime: {"prefix": target["workspace_id"] + ":"} for runtime in ("python", "node")
            }}},
        }
        saved = exchange("PUT", path, {**target, "source_key": "workspace_root_mutable_override",
            "jsonc": json.dumps(source), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": f"workspace-command-bind-{len(evidence)}"}).raise_for_status().json()["data"]
        assert saved["edit"]["state"] == "active", saved

    def publications(target: dict[str, str]) -> dict:
        return exchange("POST", "/api/gateway/service-plugins/commands/publications", target).raise_for_status().json()["data"]

    def invoke(target: dict[str, str], publication: dict, *, expected_status: int = 200) -> None:
        registration, = publication["registrations"]
        call = {**target, **{key: publication["binding"][key] for key in ("package_id", "plugin_id", "package_version")},
            "instance_id": publication["instance_id"], "registration_id": registration["registration_id"],
            "command_id": "execute", "call_id": f"workspace-call-{len(evidence)}", "remaining_ms": 5000,
            "arguments": {"mode": "configured", "value": "owner-proof"}}
        request_id = f"workspace-command-{len(evidence)}"
        response = client.post("/api/gateway/service-plugins/commands/call", json=call, headers={"X-Request-ID": request_id})
        assert response.status_code == expected_status, response.text
        assert response.headers["X-Request-ID"] == request_id
        if expected_status != 200:
            assert response.json()["request_id"] == request_id
            evidence.append({"call": call, "status": response.status_code, "body": response.json()})
            return
        events = [json.loads(line) for line in response.iter_lines()]
        assert [event["kind"] for event in events] == ["started", "progress", "result"], events
        assert [event["sequence"] for event in events] == [1, 2, 3]
        for event in events:
            assert event["request_id"] == request_id
            for key in ("instance_id", "registration_id", "command_id", "call_id"):
                assert event[key] == call[key]
        assert events[-1]["result"]["runtime"] == publication["binding"]["plugin_id"]
        assert events[-1]["result"]["value"] == target["workspace_id"] + ":owner-proof"
        evidence.append({"call": call, "events": events})

    def next_snapshot(lines: Iterator[str]) -> dict:
        return json.loads(next(line[6:] for line in lines if line.startswith("data: ")))

    try:
        for name in ("command-a", "command-b"):
            workspace = prepare_test_workspace(workspace_root=context.workspace_root / name,
                template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=())
            path = install_test_workspace_config(workspace_root=workspace,
                config_path=Path("configs/tests/workspace/default.jsonc"), schema_path=Path("configs/workspace_schema.jsonc"))
            configuration = commentjson.loads(path.read_text())
            for provider in configuration["llm"]["providers"]:
                if "api_key" in provider:
                    provider["api_key"] = "workspace-command-unused-key"
            configuration.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = str(client.base_url).rstrip("/")
            path.write_text(json.dumps(configuration), encoding="utf-8")
            registered = exchange("POST", "/api/gateway/managed-workspaces", {
                "root_path": str(workspace), "name": name, "create_directory": False,
            }).raise_for_status().json()["data"]
            item, = [item for item in registered["items"] if item["gateway_id"] == gateway_id and item["name"] == name]
            targets.append({"gateway_id": gateway_id, "workspace_id": item["workspace_id"]})

        target_a, target_b = targets
        for version, target in zip(("1.0.0", "2.0.0"), targets, strict=True):
            archive = context.artifacts_dir / f"workspace-commands-{version}.zip"
            _archive(archive, prepared_developer_tools_package, version=version, scope="workspace")
            install_plugin(lambda path, body: exchange("POST", path, body), gateway_id=gateway_id, path=str(archive))
            bind(target, version)
        snapshots = []
        for target in targets:
            # 配置 active 不等于进程 ready；沿正式贡献流等交接和完整注册。
            with client.stream("POST", "/api/gateway/service-plugins/commands/events", json=target, timeout=20) as response:
                response.raise_for_status()
                iterator, deadline = response.iter_lines(), time.monotonic() + 20
                while True:
                    snapshot = next_snapshot(iterator)
                    assert all(item["state"] != "failed" for item in snapshot["publications"]), snapshot
                    if len(snapshot["publications"]) == 2 and all(item["state"] == "ready" for item in snapshot["publications"]):
                        snapshots.append(snapshot)
                        break
                    assert time.monotonic() < deadline, snapshot
        before_a, before_b = snapshots
        for target, snapshot, version in zip(targets, (before_a, before_b), ("1.0.0", "2.0.0"), strict=True):
            assert (snapshot["gateway_id"], snapshot["workspace_id"]) == (gateway_id, target["workspace_id"])
            assert {item["binding"]["plugin_id"] for item in snapshot["publications"]} == {"python", "node"}
            for item in snapshot["publications"]:
                assert item["binding"]["workspace_id"] == target["workspace_id"] and item["binding"]["package_version"] == version
                assert item["state"] == "ready" and item["instance_id"]
                invoke(target, item)
        assert {item["instance_id"] for item in before_a["publications"]}.isdisjoint(
            {item["instance_id"] for item in before_b["publications"]})
        blocked = exchange("POST", "/api/gateway/service-plugins/uninstall", {
            "gateway_id": gateway_id, "package_id": "proof.commands", "package_version": "1.0.0"})
        assert blocked.status_code == 409 and target_a["workspace_id"] in blocked.text

        with ExitStack() as streams:
            lines = []
            for target, expected in zip(targets, (before_a, before_b), strict=True):
                response = streams.enter_context(client.stream("POST", "/api/gateway/service-plugins/commands/events",
                    json=target, timeout=15))
                response.raise_for_status()
                iterator = response.iter_lines()
                assert next_snapshot(iterator) == expected
                lines.append(iterator)
            processes = exchange("POST", "/api/gateway/service-plugins/status", {"gateway_id": gateway_id}).raise_for_status().json()["data"]["plugins"]
            removed_pids = [item["instance"]["process_id"] for item in processes if item["binding"]["workspace_id"] == target_a["workspace_id"]]
            assert len(removed_pids) == 2
            removed = exchange("DELETE", f"/api/gateway/workspaces/{target_a['workspace_id']}").raise_for_status().json()["data"]
            assert all(item["workspace_id"] != target_a["workspace_id"] for item in removed["items"])
            # 停止过程可逐项发布；登记移除后原订阅必须结束，不能留下旧注册。
            residual = [json.loads(line[6:]) for line in lines[0] if line.startswith("data: ")]
            assert all(snapshot["workspace_id"] == target_a["workspace_id"] for snapshot in residual)
            evidence.append({"removed_target": target_a, "closed_stream": residual, "removed_pids": removed_pids})
            assert all(not psutil.pid_exists(pid) for pid in removed_pids)
            assert exchange("POST", "/api/gateway/service-plugins/commands/publications", target_a).status_code == 409
            for item in before_a["publications"]:
                invoke(target_a, item, expected_status=409)
            assert publications(target_b) == before_b
            for item in before_b["publications"]:
                invoke(target_b, item)
            exchange("POST", "/api/gateway/service-plugins/uninstall", {
                "gateway_id": gateway_id, "package_id": "proof.commands", "package_version": "1.0.0"}).raise_for_status()
            # 新订阅只取得 B 原快照，不重建实例、注册或执行命令。
            reconnected = streams.enter_context(client.stream("POST", "/api/gateway/service-plugins/commands/events", json=target_b, timeout=15))
            reconnected.raise_for_status()
            assert next_snapshot(reconnected.iter_lines()) == before_b
            bind(target_b, None)
            deadline = time.monotonic() + 15
            while next_snapshot(lines[1])["publications"]:
                assert time.monotonic() < deadline, "B 解绑后原贡献未释放"
        exchange("POST", "/api/gateway/service-plugins/uninstall", {
            "gateway_id": gateway_id, "package_id": "proof.commands", "package_version": "2.0.0"}).raise_for_status()
        assert not (context.boxteam_home_for_node(request.node.nodeid) / "plugins/packages/proof.commands").exists()
        retained = exchange("GET", f"/api/v1/workspaces/{target_b['workspace_id']}/config/sources/workspace_root_mutable_override").raise_for_status().json()["data"]
        assert commentjson.loads(retained["jsonc"])["plugins"]["configuration"]["values"]["proof.commands"]["python"]["prefix"] == target_b["workspace_id"] + ":"
    finally:
        (context.artifacts_dir / "workspace-command-release.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


def test_registered_commands_progress_cancel_restart_and_disabled_admission(
    plugin_gateway: tuple[httpx.Client, TestRunContext],
    prepared_developer_tools_package: Path,
    command_snapshots: SnapshotReader,
) -> None:
    client, context = plugin_gateway
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    package = context.artifacts_dir / "commands.zip"
    _archive(package, prepared_developer_tools_package)
    install_plugin(
        lambda path, payload: client.post(path, json=payload),
        gateway_id=gateway_id,
        path=str(package.resolve()),
    )
    bindings = [{"package_id": "proof.commands", "plugin_id": runtime, "package_version": "1.0.0", "enabled": True}
                for runtime in ("python", "node")]

    submission = 0

    def bind(enabled: bool | None, *, parameters: dict[str, dict] | None = None,
             expected_status: int = 200, preflight: bool = False) -> dict[str, dict]:
        nonlocal submission
        submission += 1
        source_path = "/api/gateway/config/sources/gateway_mutable_override"
        document = client.get(source_path).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source.setdefault("plugins", {})["runtime"] = {"entries": [] if enabled is None else [{**item, "enabled": enabled} for item in bindings]}
        if parameters is not None:
            source["plugins"]["configuration"] = {"values": {"proof.commands": parameters}}
        intent = {"gateway_id": gateway_id, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(source), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": f"commands-{submission}"}
        response = client.post(source_path + "/preflight", json=intent) if preflight else client.put(source_path, json=intent)
        assert response.status_code == expected_status, response.text
        if expected_status != 200:
            assert client.get(source_path).raise_for_status().json()["data"]["jsonc"] == document["jsonc"]
        return {item["binding"]["plugin_id"]: item for item in client.post("/api/gateway/service-plugins/status",
            json={"gateway_id": gateway_id}).raise_for_status().json()["data"]["plugins"]}

    states = bind(True)
    assert set(states) == {"python", "node"}
    published = command_snapshots(lambda snapshot: len(snapshot["publications"]) == 2
        and all(item["state"] == "ready" for item in snapshot["publications"]))
    assert {item["binding"]["plugin_id"] for item in published["publications"]} == {"python", "node"}
    for item in published["publications"]:
        assert item["instance_id"] == states[item["binding"]["plugin_id"]]["instance"]["instance_id"]
        assert [entry["command_id"] for entry in item["registrations"]] == ["execute"]
        assert item["commands"][0]["title"] == "Execute"
        assert not ({"parameters", "base_url", "token", "process_id"} & item.keys())
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
    assert command_snapshots(lambda snapshot: not snapshot["publications"])["publications"] == []
    assert all(state["state"] == "stopped" for state in disabled.values())
    assert client.post("/api/gateway/service-plugins/commands/call", json=last_call).status_code == 409

    # 关闭状态仍严格校验参数；编辑不能隐式创建实例。
    bind(False, parameters={"python": {"prefix": 7}}, expected_status=422, preflight=True)
    bind(False, parameters={"python": {"credentialValue": "must-not-be-published"}}, expected_status=422, preflight=True)
    configured = bind(False, parameters={runtime: {"prefix": runtime + ':'} for runtime in ("python", "node")})
    assert all(item["state"] == "stopped" and item["instance"] is None for item in configured.values())
    enabled = bind(True)

    def configured_call(runtime: str, state: dict, value: str) -> str:
        identity = {"gateway_id": gateway_id, "package_id": "proof.commands", "package_version": "1.0.0",
            "plugin_id": runtime, "instance_id": state["instance"]["instance_id"]}
        registration = client.post("/api/gateway/service-plugins/commands/registrations", json=identity).raise_for_status().json()["data"]["commands"][0]
        response = client.post("/api/gateway/service-plugins/commands/call", json={**identity,
            "registration_id": registration["registration_id"], "command_id": "execute",
            "call_id": f"{runtime}-parameters-{submission}-{value}", "remaining_ms": 5000,
            "arguments": {"mode": "configured", "value": value}}).raise_for_status()
        return json.loads(list(response.iter_lines())[-1])["result"]["value"]

    for runtime in ("python", "node"):
        assert configured_call(runtime, enabled[runtime], "first") == runtime + ':first'
    changed = bind(True, parameters={runtime: {"prefix": 'new:'} for runtime in ("python", "node")})
    for runtime in ("python", "node"):
        assert changed[runtime]["instance"]["instance_id"] != enabled[runtime]["instance"]["instance_id"]
        assert configured_call(runtime, changed[runtime], "second") == 'new:second'
        current = changed[runtime]
        restarted = client.post("/api/gateway/service-plugins/restart", json={"gateway_id": gateway_id,
            "package_id": "proof.commands", "package_version": "1.0.0", "plugin_id": runtime,
            "revision": current["revision"]}).raise_for_status().json()["data"]
        assert configured_call(runtime, restarted, "restart") == 'new:restart'

    # 两种语言的日志生产者可同名，仍按真实 PID／实例分别确认，热应用不能重启插件。
    def command_logging() -> dict[int, dict]:
        diagnostics = client.get("/api/gateway/diagnostics").raise_for_status().json()["data"]
        processes = [item for item in diagnostics["processes"] if item["service"].startswith("proof.commands/")]
        assert len(processes) == 2 and all(item["error"] is None for item in processes), processes
        return {item["state"]["pid"]: item["state"] for item in processes}

    before_logging = command_logging()
    assert {state["service"] for state in before_logging.values()} == {"command-proof"}
    source_path = "/api/gateway/config/sources/gateway_mutable_override"
    document = client.get(source_path).raise_for_status().json()["data"]
    source = commentjson.loads(document["jsonc"])
    source["logging"] = {"runtime": {"level": "warning", "pretty": False}}
    saved = client.put(source_path, json={"gateway_id": gateway_id, "source_key": "gateway_mutable_override",
        "jsonc": json.dumps(source), "base_file_digest": document["file_digest"],
        "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
        "idempotency_key": "commands-logging"}).raise_for_status().json()["data"]
    assert saved["edit"]["state"] == "active", saved
    after_logging = command_logging()
    assert after_logging.keys() == before_logging.keys()
    for pid, state in after_logging.items():
        assert state == before_logging[pid] | {"level": "warning", "pretty": False,
            "applied_revision": saved["active_digest"]}

    latest = client.post("/api/gateway/service-plugins/commands/publications",
                         json={"gateway_id": gateway_id}).raise_for_status().json()["data"]
    current = {item["binding"]["plugin_id"]: item for item in latest["publications"]}
    with client.stream("POST", "/api/gateway/service-plugins/commands/events", json={"gateway_id": gateway_id}) as late:
        late.raise_for_status()
        first = next(line for line in late.iter_lines() if line.startswith("data: "))
        assert json.loads(first[6:]) == latest
    node_instance = current["node"]["instance_id"]
    actual = client.post("/api/gateway/service-plugins/status", json={"gateway_id": gateway_id})\
        .raise_for_status().json()["data"]["plugins"]
    python_pid = next(item["instance"]["process_id"] for item in actual if item["binding"]["plugin_id"] == "python")
    psutil.Process(python_pid).kill()
    failed = command_snapshots(lambda snapshot: any(item["binding"]["plugin_id"] == "python"
        and item["state"] == "failed" for item in snapshot["publications"]))
    failures = {item["binding"]["plugin_id"]: item for item in failed["publications"]}
    assert failures["python"]["registrations"] == [] and failures["python"]["instance_id"] is None
    assert failures["python"]["error"] and failures["node"]["instance_id"] == node_instance
    assert failures["node"]["state"] == "ready" and len(failures["node"]["registrations"]) == 1

    # 解绑残留不进入有效快照，不引用包；再绑定必须使用当前包 schema。
    assert bind(None) == {}
    bind(None, parameters={"python": {"prefix": 7}})
    document = client.get("/api/gateway/config/sources/gateway_mutable_override").raise_for_status().json()["data"]
    assert document["source_effective"]["plugins"]["configuration"]["values"]["proof.commands"]["python"]["prefix"] == 7
    assert "configuration" not in document["effective"]["plugins"]
    bind(True, expected_status=422, preflight=True)
    client.post("/api/gateway/service-plugins/uninstall", json={"gateway_id": gateway_id,
        "package_id": "proof.commands", "package_version": "1.0.0"}).raise_for_status()
    retained = client.get("/api/gateway/config/sources/gateway_mutable_override").raise_for_status().json()["data"]
    assert retained["jsonc"] == document["jsonc"] and "configuration" not in retained["effective"]["plugins"]
    (context.artifacts_dir / "command-events.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
