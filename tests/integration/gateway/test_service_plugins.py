"""经真实 Gateway 安装与切换自有包，受控插件执行真实 HTTP 进程。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path

import commentjson
import httpx
import psutil
import pytest

from app.schemas.gateway.service_plugin import PluginSlotStatusDTO
from tests.harness.python.run_context import TestRunContext
from tests.support.plugin_package import (
    expect_plugin_install_failure,
    install_plugin,
    native_plugin_target,
)
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import install_test_workspace_config

PYTHON_PLUGIN = '''
import asyncio, json, os, sys
import local_module
sys.path.insert(0, os.environ['BOXTEAM_PROJECT_ROOT'])
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
from app.core.logging_config import configure_application_logging
from app.core.trace_middleware import TraceMiddleware
from app.runtime.plugin_commands import plugin_command_router
configure_application_logging(service='lifecycle-probe')
identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])
mode = sys.argv[1] if len(sys.argv) > 1 else ''
async def shutdown(): server.should_exit = True
app = FastAPI()
app.add_middleware(TraceMiddleware)
@app.middleware('http')
async def controlled_fault(request, call_next):
    if request.headers.get('X-Local-Token') != os.environ['BOXTEAM_RUNTIME_CONTROL_TOKEN']:
        return JSONResponse({'detail': 'invalid token'}, status_code=403)
    if request.url.path == '/internal/plugin/status' and mode in ('bad-ready', 'bad-capabilities'):
        return JSONResponse({**identity, 'status': 'ready', 'process_id': os.getpid(),
            'plugin_id': 'wrong-plugin' if mode == 'bad-ready' else identity['plugin_id'],
            'capabilities': ['status', 'shutdown'], 'log_sink': json.loads(os.environ['BOXTEAM_RUNTIME_LOG_SINK'])})
    if request.url.path == '/internal/plugin/shutdown' and mode == 'bad-shutdown':
        return JSONResponse({'detail': 'probe refuses shutdown'}, status_code=500)
    return await call_next(request)
app.include_router(plugin_command_router({}, shutdown=shutdown))
@app.get('/services/echo/{resource:path}')
async def echo(request: Request, resource: str):
    if resource == 'failure': return JSONResponse({'detail': 'probe failure'}, status_code=500)
    return {'pid': os.getpid(), 'instance_id': identity['instance_id'],
            'version': identity['package_version'], 'cwd': os.getcwd(),
            'request_id': request.headers.get('X-Request-ID')}
@app.post('/services/echo/crash')
async def crash(): os._exit(17)
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(os.environ['BOXTEAM_PLUGIN_PORT']), log_config=None))
asyncio.run(server.serve())
'''

NODE_PLUGIN = '''
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const source = name => pathToFileURL(path.join(process.env.BOXTEAM_PROJECT_ROOT, 'src/shared', name)).href;
const { createPluginRuntimeManagement } = await import(source('serverPluginRuntime.mjs'));
const { createRuntimeLogger } = await import(source('serverRuntimeLogging.mjs'));
const logger = createRuntimeLogger('lifecycle-probe');
const readJson = async request => { let body=''; for await(const chunk of request) body+=chunk; return body ? JSON.parse(body) : {}; };
const management = createPluginRuntimeManagement({ service:'lifecycle-probe', logger, readJson,
  shutdown: () => new Promise((resolve,reject) => server.close(error => error ? reject(error) : resolve())) });
const identity = management.identity;
const server = http.createServer(async(req,res) => {
  try {
    if(await management.handleRequest(req,res,new URL(req.url,'http://localhost').pathname)) return;
    if(req.headers['x-local-token']!==management.token){res.writeHead(403);res.end();return;}
    res.writeHead(200,{'Content-Type':'application/json'});
    res.end(JSON.stringify({pid:process.pid,instance_id:identity.instance_id,
      version:identity.package_version,request_id:req.headers['x-request-id']}));
  } catch(error) { logger.error('probe request failed',error); res.writeHead(error.statusCode ?? 500);res.end('{}'); }
});
server.listen(management.port,'127.0.0.1');
'''


@pytest.fixture
def plugin_gateway(request: pytest.FixtureRequest, integration_workspace_root_path: str, unused_tcp_port: int
) -> Iterator[tuple[httpx.Client, TestRunContext]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    config_path = install_test_workspace_config(workspace_root=workspace,
        config_path=Path("configs/tests/workspace/default.jsonc"), schema_path=Path("configs/workspace_schema.jsonc"))
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    for provider in config["llm"]["providers"]:
        if "api_key" in provider:
            provider["api_key"] = "plugin-lifecycle-unused-key"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None,
        port=unused_tcp_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=30, trust_env=False) as client:
            yield client, context
    finally:
        close_gateway_process(gateway)


def test_multi_plugin_package_install_enable_restart_switch_and_uninstall(
    plugin_gateway: tuple[httpx.Client, TestRunContext],
    request: pytest.FixtureRequest,
) -> None:
    client, context = plugin_gateway
    evidence: list[dict[str, object]] = []
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    target = {"gateway_id": gateway_id}

    def exchange(method: str, path: str, body: dict[str, object] | None = None) -> httpx.Response:
        request_id = f"plugin-flow-{len(evidence)}"
        response = client.request(method, path, json=body, headers={"X-Request-ID": request_id})
        assert response.headers["X-Request-ID"] == request_id
        payload = response.json()
        assert payload.get("request_id") == request_id
        evidence.append({"path": path, "status": response.status_code, "body": payload})
        return response

    def inventory() -> dict[str, object]:
        return exchange("POST", "/api/gateway/service-plugins/inventory", target).raise_for_status().json()["data"]

    def states() -> dict[str, dict[str, object]]:
        data = exchange("POST", "/api/gateway/service-plugins/status", target).raise_for_status().json()["data"]
        return {item["binding"]["plugin_id"]: item for item in data["plugins"]}

    workspace_id = exchange("GET", "/api/gateway/workspaces").raise_for_status().json()["data"]["items"][0]["workspace_id"]

    def skill_versions() -> dict[str, str]:
        data = exchange("GET", f"/api/gateway/skill-sources/{gateway_id}/workspaces/{workspace_id}").raise_for_status().json()["data"]
        return {item["plugin"]["plugin_id"]: item["plugin"]["package_version"]
            for item in data["entries"] if item.get("plugin") and item["plugin"]["package_id"] == "probe.tools"}

    def bind(entries: list[dict[str, object]], parameters: dict[str, object] | None = None) -> dict[str, object]:
        path = "/api/gateway/config/sources/gateway_mutable_override"
        document = exchange("GET", path).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source.setdefault("plugins", {})["runtime"] = {"entries": entries}
        if parameters is not None:
            source["plugins"]["configuration"] = {"values": {"probe.tools": {"python": parameters}}}
        reply = exchange("PUT", path, {"gateway_id": gateway_id, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(source), "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": f"plugin-bind-{len(evidence)}"})
        reply.raise_for_status()
        assert reply.json()["data"]["edit"]["state"] == "active"
        return states()

    def entries(version: str = "1.0.0", *, python_enabled: bool = True) -> list[dict[str, object]]:
        return [{"package_id": "probe.tools", "package_version": version, "plugin_id": plugin,
            "enabled": python_enabled if plugin == "python" else True} for plugin in ("python", "node", "declarations")]

    def package(version: str, mode: str = '', *, target: dict[str, object] | None = None) -> Path:
        path = context.artifacts_dir / f"probe-{version}.zip"
        manifest = {"manifest_version": 1, "package_id": "probe.tools", "version": version,
            "target": native_plugin_target("python", "node") if target is None else target,
            "name": "真实受控插件", "extension_contract_version": 1, "plugins": [
                {"id": runtime, "name": runtime, "instance_scope": "gateway",
                 "requires": [{"extension_point": "services", "version": 1},
                    {"extension_point": "configuration", "version": 1},
                    {"extension_point": "skills", "version": 1}],
                 "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}],
                    "skills": [{"id": "instructions", "path": f"skills/{runtime}"}],
                    "configuration": {"type": "object", "additionalProperties": False,
                        "properties": {"credentialValue": {"type": "string", "minLength": 8, "writeOnly": True}}}},
                 "runtime": runtime, "entrypoint": ["nested/custom/entry.py", mode] if runtime == "python" else ["another/plugin.mjs"],
                 "management_protocol_version": 1} for runtime in ("python", "node")] + [
                {"id": "declarations", "name": "无进程配置", "instance_scope": "gateway",
                 "requires": [{"extension_point": "configuration", "version": 1}],
                 "contributes": {"configuration": {"type": "object", "properties": {}}}}]}
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("plugin.jsonc", json.dumps(manifest))
            archive.writestr("nested/custom/entry.py", PYTHON_PLUGIN.replace(
                "'version': identity['package_version'], 'cwd': os.getcwd(),",
                "'credential_digest': __import__('hashlib').sha256(json.loads(os.environ['BOXTEAM_PLUGIN_PARAMETERS']).get('credentialValue', '').encode()).hexdigest(), 'version': identity['package_version'], 'cwd': os.getcwd(),"))
            archive.writestr("nested/custom/local_module.py", "VALUE = 1\n")
            archive.writestr("another/plugin.mjs", NODE_PLUGIN)
            for runtime in ("python", "node"):
                archive.writestr(f"skills/{runtime}/SKILL.md",
                    f"---\nname: {runtime}-lifecycle-probe\ndescription: 插件生命周期验证\n---\n{version}\n")
        return path

    def install(version: str, mode: str = '') -> None:
        path = context.artifacts_dir / f"probe-{version}.zip"
        if not path.exists():
            path = package(version, mode)
        install_plugin(
            lambda endpoint, payload: exchange("POST", endpoint, payload),
            gateway_id=gateway_id,
            path=str(path),
            expected_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def echo(item: dict[str, object]) -> httpx.Response:
        binding, instance = item["binding"], item["instance"]
        path = f"/api/gateway/service-plugins/probe.tools/{binding['plugin_id']}/instances/{instance['instance_id']}/services/echo/value"
        return exchange("GET", path + "?package_version=" + binding["package_version"])

    def installed_content() -> dict[str, str]:
        root = context.boxteam_home_for_node(request.node.nodeid) / "plugins/packages"
        return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}

    try:
        assert inventory()["sources"]["configured"] is False
        install("1.0.0")
        assert states() == {}, "安装与库存读取不能启动插件"
        assert skill_versions() == {}, "安装不能发布未开启插件的说明"
        install("1.0.0")
        assert len(inventory()["packages"]) == 1
        original_content = installed_content()
        current = bind(entries())
        assert current["declarations"]["instance"] is None and current["declarations"]["declarative"] is True
        python, node = current["python"], current["node"]
        assert python["state"] == node["state"] == "ready"
        assert skill_versions() == {"python": "1.0.0", "node": "1.0.0"}
        assert python["instance"]["process_id"] != node["instance"]["process_id"]
        assert echo(python).raise_for_status().json()["version"] == "1.0.0"
        assert echo(node).raise_for_status().json()["version"] == "1.0.0"
        assert "/plugins/packages/" not in echo(python).json()["cwd"]
        for index, field in enumerate(("version", "abi")):
            target_environment = json.loads(json.dumps(native_plugin_target("python", "node")))
            target_environment["node"][field] = "0.0.0" if field == "version" else "0"
            incompatible = package(f"6.0.{index}", target=target_environment)
            rejected = expect_plugin_install_failure(
                lambda endpoint, payload: exchange("POST", endpoint, payload),
                gateway_id=gateway_id, path=str(incompatible),
                expected_digest=hashlib.sha256(incompatible.read_bytes()).hexdigest(),
            )
            assert rejected.error and "Node 运行时不匹配" in rejected.error
            assert "package=" in rejected.error and "gateway=" in rejected.error and "完整插件 ZIP" in rejected.error
            assert states() == current, "不兼容制品不能改变旧绑定、revision或实际实例"
            assert echo(python).raise_for_status().json()["version"] == "1.0.0"
            assert echo(node).raise_for_status().json()["version"] == "1.0.0"
        assert installed_content() == original_content, "不兼容制品不能发布或修改原安装内容"
        private_value = "gateway-private-input-sentinel"
        private_request = {**target, "package_id": "probe.tools", "plugin_id": "python", "package_version": "1.0.0",
            "source_key": "gateway_mutable_override", "parameter_pointer": "/credentialValue", "value": private_value}
        for invalid in ({"parameter_pointer": "/undeclared"}, {"package_version": "missing"}, {"source_key": "inline:0"},
                        {"value": "short"}):
            rejected = exchange("POST", "/api/gateway/service-plugins/private-inputs", {**private_request, **invalid})
            assert rejected.status_code == 409 and private_value not in rejected.text
        private_response = exchange("POST", "/api/gateway/service-plugins/private-inputs", private_request).raise_for_status()
        assert private_response.headers["Cache-Control"] == "no-store" and private_value not in private_response.text
        reference = private_response.json()["data"]
        current = bind(entries(), {"credentialValue": reference})
        python = current["python"]
        assert current["node"]["instance"] == node["instance"]
        digest = hashlib.sha256(private_value.encode()).hexdigest()
        assert echo(python).raise_for_status().json()["credential_digest"] == digest
        source_document = exchange("GET", "/api/gateway/config/sources/gateway_mutable_override").json()["data"]
        assert private_value not in json.dumps(source_document)
        assert private_value not in json.dumps(states())
        private_path = (context.boxteam_home_for_node(request.node.nodeid)
            / "credentials/plugins" / f"{reference['version']}.json")
        unavailable_private_path = private_path.with_suffix(".unavailable")
        before_missing_input = states()
        before_python = before_missing_input["python"]
        private_path.rename(unavailable_private_path)
        try:
            rejected = exchange("POST", "/api/gateway/service-plugins/restart", {**target,
                "package_id": "probe.tools", "package_version": "1.0.0", "plugin_id": "python",
                "revision": before_python["revision"]})
            assert rejected.status_code == 409 and "私有输入版本不存在" in rejected.text
            assert states() == before_missing_input, "重启预校验失败不能改变原绑定、revision或实际实例"
            assert echo(before_python).raise_for_status().json()["credential_digest"] == digest
        finally:
            unavailable_private_path.rename(private_path)
        blocked = exchange("POST", "/api/gateway/service-plugins/uninstall",
            {**target, "package_id": "probe.tools", "package_version": "1.0.0"})
        assert blocked.status_code == 409
        restarted = exchange("POST", "/api/gateway/service-plugins/restart", {**target,
            "package_id": "probe.tools", "package_version": "1.0.0", "plugin_id": "python", "revision": python["revision"]})
        restarted.raise_for_status()
        python_new = restarted.json()["data"]
        assert echo(python_new).raise_for_status().json()["credential_digest"] == digest
        assert python_new["instance"]["instance_id"] != python["instance"]["instance_id"]
        assert not psutil.pid_exists(python["instance"]["process_id"])
        assert echo(python).status_code == 409
        assert states()["node"]["instance"] == node["instance"]
        assert installed_content() == original_content, "运行、导入本地模块和重启不能修改已安装包"
        crash_path = (f"/api/gateway/service-plugins/probe.tools/python/instances/"
            f"{python_new['instance']['instance_id']}/services/echo/crash?package_version=1.0.0")
        assert client.post(crash_path).status_code == 502
        assert states()["python"]["state"] == "failed"
        assert not psutil.pid_exists(python_new["instance"]["process_id"])
        assert states()["python"]["binding"]["enabled"] is True
        assert skill_versions()["python"] == "1.0.0", "运行崩溃不能撤销已发布的静态说明"
        assert echo(python_new).status_code == 409
        assert states()["node"]["instance"] == node["instance"]
        recovered = exchange("POST", "/api/gateway/service-plugins/restart", {**target,
            "package_id": "probe.tools", "package_version": "1.0.0", "plugin_id": "python",
            "revision": python_new["revision"]}).raise_for_status().json()["data"]
        assert recovered["state"] == "ready" and recovered["instance"] != python_new["instance"]
        install("2.0.0")
        assert states()["python"]["binding"]["package_version"] == "1.0.0"
        assert skill_versions()["python"] == "1.0.0", "安装新版不能切换说明版本"
        switched = bind(entries("2.0.0"))
        assert skill_versions() == {"python": "2.0.0", "node": "2.0.0"}
        assert echo(switched["python"]).raise_for_status().json()["version"] == "2.0.0"
        assert echo(python_new).status_code == 409
        disabled = bind(entries("2.0.0", python_enabled=False))
        assert disabled["python"]["state"] == "stopped"
        assert not psutil.pid_exists(switched["python"]["instance"]["process_id"])
        assert echo(switched["python"]).status_code == 409
        assert states()["python"]["binding"]["enabled"] is False
        assert skill_versions() == {"node": "2.0.0"}, "关闭插件须撤销新说明准入"
        before_failure = bind(entries("2.0.0"))["python"]
        install("3.0.0", "bad-ready")
        failed = bind(entries("3.0.0"))
        assert failed["python"]["state"] == "failed"
        assert failed["python"]["binding"]["enabled"] is True
        assert failed["python"]["log_id"] and failed["python"]["instance"] is None
        diagnostic = client.get("/api/gateway/diagnostics", params={
            "log_id": failed["python"]["log_id"], "tail_lines": 30,
        }).raise_for_status().json()["data"]
        assert diagnostic["selected_log_id"] == failed["python"]["log_id"]
        failed_log, = [log for log in diagnostic["logs"] if log["log_id"] == failed["python"]["log_id"]]
        assert failed_log["status"] in {"available", "empty"}, failed_log
        assert failed["node"]["state"] == "ready", "单个插件失败不能阻断兄弟启动"
        assert not psutil.pid_exists(before_failure["instance"]["process_id"])
        assert echo(before_failure).status_code == 409
        assert skill_versions() == {"node": "3.0.0"}, "新版启动失败必须停止该绑定后续贡献发布"
        assert bind(entries("3.0.0"))["python"]["instance"] is None, "相同绑定读取或提交不得重试启动"
        assert skill_versions() == {"node": "3.0.0"}, "相同配置不能补发失败版本的贡献"
        install("5.0.0", "bad-capabilities")
        incomplete = bind(entries("5.0.0"))
        assert incomplete["python"]["state"] == "failed" and incomplete["python"]["instance"] is None
        assert "observation" in incomplete["python"]["error"]
        assert incomplete["node"]["state"] == "ready", "缺少必需管理能力只拒绝原插件"
        assert skill_versions() == {"node": "5.0.0"}
        assert bind(entries("5.0.0"))["python"]["instance"] is None, "同绑定不自动重试协议拒绝"
        install("4.0.0", "bad-shutdown")
        running = bind(entries("4.0.0"))["python"]
        assert running["state"] == "ready"
        stop_failed = bind(entries("4.0.0", python_enabled=False))["python"]
        assert stop_failed["state"] == "failed" and "shutdown" in stop_failed["error"]
        stop_status = PluginSlotStatusDTO.model_validate(stop_failed)
        assert stop_status.binding.enabled is False, "停止失败不能把已提交关闭显示为开启"
        assert stop_status.instance == PluginSlotStatusDTO.model_validate(running).instance, "失败诊断须保留原实际实例回执"
        assert not psutil.pid_exists(running["instance"]["process_id"]), "管理响应失败也须核实实际进程退出"
        unchanged = PluginSlotStatusDTO.model_validate(bind(entries("4.0.0", python_enabled=False))["python"])
        assert unchanged.state == "failed" and unchanged.error == stop_status.error, "相同配置不能重试或洗掉停止失败回执"
        running_again = PluginSlotStatusDTO.model_validate(bind(entries("4.0.0"))["python"])
        assert running_again.instance is not None
        replacement_failed = PluginSlotStatusDTO.model_validate(bind(entries("1.0.0"))["python"])
        assert replacement_failed.state == "failed" and "shutdown" in str(replacement_failed.error)
        assert replacement_failed.binding.package_version == "1.0.0", "替换失败仍须展示已提交的期望版本"
        assert replacement_failed.instance == running_again.instance, "旧版本实际回执不能改写成新版本"
        assert echo(running_again.model_dump(mode="json")).status_code == 409 and skill_versions() == {"node": "1.0.0"}
        assert bind(entries("1.0.0"))["python"]["state"] == "failed", "无关配置提交不能继续未完成的换版"
        explicitly_restarted = exchange("POST", "/api/gateway/service-plugins/restart", {**target,
            "package_id": "probe.tools", "package_version": "1.0.0", "plugin_id": "python",
            "revision": replacement_failed.revision}).raise_for_status().json()["data"]
        assert explicitly_restarted["state"] == "ready"
        assert echo(explicitly_restarted).raise_for_status().json()["version"] == "1.0.0"
        assert not psutil.pid_exists(running_again.instance.process_id)
        assert bind([]) == {}, "解除全部绑定后不得继续展示幽灵插件"
        assert skill_versions() == {}
        for version in ("1.0.0", "2.0.0", "3.0.0", "4.0.0", "5.0.0"):
            exchange("POST", "/api/gateway/service-plugins/uninstall", {**target,
                "package_id": "probe.tools", "package_version": version}).raise_for_status()
        assert inventory()["packages"] == []
    finally:
        (context.artifacts_dir / "plugin-flow.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("shared_package", [False, True], ids=["separate-packages", "same-package"])
@pytest.mark.parametrize("runtime_change", ["missing", "different-version"])
def test_saved_plugin_without_runtime_reports_failure_and_preserves_other_bindings(
    request: pytest.FixtureRequest, integration_workspace_root_path: str, unused_tcp_port: int,
    shared_package: bool, runtime_change: str,
) -> None:
    replacement_node = ""
    expected_error = "BOXTEAM_NODE_BIN"
    replacement_identity = None
    if runtime_change == "different-version":
        alternate = os.environ.get("BOXTEAM_TEST_ALTERNATE_NODE_BIN")
        if not alternate:
            pytest.skip("需要 BOXTEAM_TEST_ALTERNATE_NODE_BIN 提供版本和 ABI 不同的真实 Node")
        replacement_node = str(Path(alternate).resolve(strict=True))
        replacement_identity = json.loads(subprocess.run([replacement_node, "--print",
            "JSON.stringify({version:process.versions.node,abi:process.versions.modules})"],
            text=True, capture_output=True, check=True, timeout=5).stdout)
        original_identity = native_plugin_target("node")["node"]
        assert isinstance(original_identity, dict)
        assert all(replacement_identity[key] != original_identity[key] for key in ("version", "abi")), (
            original_identity, replacement_identity)
        expected_error = "插件 Node 运行时不匹配"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    home = context.boxteam_home_for_node(request.node.nodeid)
    config_path = install_test_workspace_config(workspace_root=workspace,
        config_path=Path("configs/tests/workspace/default.jsonc"), schema_path=Path("configs/workspace_schema.jsonc"))
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    for provider in config["llm"]["providers"]:
        if "api_key" in provider:
            provider["api_key"] = "cold-runtime-unused-key"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    gateway = None
    bindings = [{"package_id": "cold.tools" if shared_package else f"cold.{runtime}", "package_version": "1.0.0",
        "plugin_id": runtime, "enabled": True} for runtime in ("node", "python")]
    source_path = "/api/gateway/config/sources/gateway_mutable_override"

    def save(client: httpx.Client, document: dict, enabled: bool | None, *, preflight: bool = False) -> httpx.Response:
        config = commentjson.loads(document["jsonc"])
        config.setdefault("plugins", {})["runtime"] = {"entries": [] if enabled is None else [
            {**binding, "enabled": enabled if binding["plugin_id"] == "node" else True}
            for binding in bindings]}
        return client.request("POST" if preflight else "PUT", source_path + ("/preflight" if preflight else ""),
            json={"gateway_id": gateway_id,
            "source_key": "gateway_mutable_override", "jsonc": json.dumps(config),
            "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
            "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
            "expected_active_digest": document["active_digest"],
            "idempotency_key": f"cold-binding-{document['file_digest']}-{enabled}"})

    def states(client: httpx.Client) -> dict:
        response = client.post("/api/gateway/service-plugins/status", json={"gateway_id": gateway_id})
        return {item["binding"]["plugin_id"]: item for item in response.raise_for_status().json()["data"]["plugins"]}

    try:
        gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None,
            port=unused_tcp_port, boxteam_home=home, artifacts_dir=context.artifacts_dir)
        with httpx.Client(base_url=f"http://127.0.0.1:{unused_tcp_port}", headers=LOCAL_TOKEN_HEADERS,
                          timeout=30, trust_env=False) as client:
            gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
            packages = [("cold.tools", ("node", "python"))] if shared_package else [
                (f"cold.{runtime}", (runtime,)) for runtime in ("node", "python")]
            for package_id, runtimes in packages:
                archive_path = context.artifacts_dir / f"{package_id}.zip"
                manifest = {"manifest_version": 1, "package_id": package_id, "version": "1.0.0",
                    "name": package_id, "target": native_plugin_target(*runtimes), "extension_contract_version": 1,
                    "plugins": [{"id": runtime, "name": runtime, "instance_scope": "gateway",
                        "runtime": runtime, "entrypoint": ["entry.mjs" if runtime == "node" else "entry.py"],
                        "management_protocol_version": 1,
                        "requires": [{"extension_point": "services", "version": 1}],
                        "contributes": {"services": [{"service_id": "echo", "service_type": "terminal",
                            "protocol": "http"}]}} for runtime in runtimes]}
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr("plugin.jsonc", json.dumps(manifest))
                    if "node" in runtimes:
                        archive.writestr("entry.mjs", NODE_PLUGIN)
                    if "python" in runtimes:
                        archive.writestr("entry.py", PYTHON_PLUGIN)
                        archive.writestr("local_module.py", "VALUE = 1\n")
                install_plugin(lambda endpoint, payload: client.post(endpoint, json=payload),
                    gateway_id=gateway_id, path=str(archive_path),
                    expected_digest=hashlib.sha256(archive_path.read_bytes()).hexdigest())
            document = client.get(source_path).raise_for_status().json()["data"]
            assert save(client, document, True).raise_for_status().json()["data"]["edit"]["state"] == "active"
            initial = states(client)
            assert {item["state"] for item in initial.values()} == {"ready"}
            saved = client.get(source_path).raise_for_status().json()["data"]
            client.get("/api/gateway/workspaces", timeout=90).raise_for_status()

        close_gateway_process(gateway)
        gateway = None
        assert all(not psutil.pid_exists(item["instance"]["process_id"]) for item in initial.values())
        # 保存的包和 enabled 来自产品入口；冷启动实际移除或更换目标 Node。
        gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None,
            port=unused_tcp_port, boxteam_home=home, artifacts_dir=context.artifacts_dir,
            extra_env={"BOXTEAM_NODE_BIN": replacement_node})
        with httpx.Client(base_url=f"http://127.0.0.1:{unused_tcp_port}", headers=LOCAL_TOKEN_HEADERS,
                          timeout=30, trust_env=False) as client:
            current = states(client)
            assert current["node"]["binding"] == bindings[0] | {"workspace_id": None}
            assert current["node"]["state"] == "failed" and current["node"]["instance"] is None
            assert expected_error in current["node"]["error"]
            if replacement_identity is not None:
                assert f"gateway={replacement_identity['version']}/{replacement_identity['abi']}" in current["node"]["error"]
                assert "package=" in current["node"]["error"] and "完整插件 ZIP" in current["node"]["error"]
            python = current["python"]
            assert python["state"] == "ready"
            assert python["instance"]["instance_id"] != initial["python"]["instance"]["instance_id"]
            echo_path = (f"/api/gateway/service-plugins/{bindings[1]['package_id']}/python/instances/"
                f"{python['instance']['instance_id']}/services/echo/value?package_version=1.0.0")
            assert client.get(echo_path).raise_for_status().json()["pid"] == python["instance"]["process_id"]
            document = client.get(source_path).raise_for_status().json()["data"]
            assert document["jsonc"] == saved["jsonc"]
            assert document["active_revision"] == saved["active_revision"]
            failed_restart = client.post("/api/gateway/service-plugins/restart", json={
                "gateway_id": gateway_id, "package_id": bindings[0]["package_id"], "package_version": "1.0.0",
                "plugin_id": "node", "revision": current["node"]["revision"]})
            assert failed_restart.status_code == 409 and expected_error in failed_restart.text
            assert states(client) == current, "被拒的显式重启不能改变原绑定、revision或实际实例"
            disabled = save(client, document, False).raise_for_status().json()["data"]
            assert disabled["edit"]["state"] == "active"
            assert states(client)["node"]["state"] == "stopped"
            document = client.get(source_path).raise_for_status().json()["data"]
            preflight = save(client, document, True, preflight=True)
            assert preflight.status_code == 422 and expected_error in preflight.text
            rejected = save(client, document, True)
            assert rejected.status_code == 422 and expected_error in rejected.text
            assert client.get(source_path).raise_for_status().json()["data"]["file_digest"] == document["file_digest"]
            final = states(client)
            assert final["node"]["binding"]["enabled"] is False
            assert final["python"]["instance"] == python["instance"], "失败候选不能停止仍可用的兄弟实例"
            blocked = client.post("/api/gateway/service-plugins/uninstall", json={
                "gateway_id": gateway_id, "package_id": bindings[0]["package_id"], "package_version": "1.0.0"})
            assert blocked.status_code == 409 and "引用" in blocked.text, "失败或已关闭的配置绑定仍阻止卸载"
            document = client.get(source_path).raise_for_status().json()["data"]
            assert save(client, document, None).raise_for_status().json()["data"]["edit"]["state"] == "active"
            assert states(client) == {}
            assert not psutil.pid_exists(python["instance"]["process_id"])
            for package_id in {binding["package_id"] for binding in bindings}:
                client.post("/api/gateway/service-plugins/uninstall", json={
                    "gateway_id": gateway_id, "package_id": package_id, "package_version": "1.0.0"}).raise_for_status()
                assert not (home / "plugins/packages" / package_id / "1.0.0").exists()
            inventory = client.post("/api/gateway/service-plugins/inventory", json={"gateway_id": gateway_id})
            assert inventory.raise_for_status().json()["data"]["packages"] == []
            client.get("/api/gateway/workspaces", timeout=90).raise_for_status()
            (context.artifacts_dir / "cold-runtime.json").write_text(json.dumps({
                "initial": initial, "unavailable": current, "final": final,
                "runtime_change": runtime_change, "replacement_node": replacement_node,
                "replacement_identity": replacement_identity, "uninstalled": True}, ensure_ascii=False, indent=2))
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
