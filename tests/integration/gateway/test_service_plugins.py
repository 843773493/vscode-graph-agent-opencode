"""经真实 Gateway 安装与切换自有包，受控插件执行真实 HTTP 进程。"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import commentjson
import httpx
import psutil
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)

PYTHON_PLUGIN = '''
import json, os, sys, threading
import local_module
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])
identity.update(status='ready', process_id=os.getpid(), capabilities=['status','shutdown'],
    log_sink=json.loads(os.environ['BOXTEAM_RUNTIME_LOG_SINK']))
mode = sys.argv[1] if len(sys.argv) > 1 else ''
if mode == 'bad-ready': identity['plugin_id'] = 'wrong-plugin'
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('X-Local-Token') != os.environ['BOXTEAM_RUNTIME_CONTROL_TOKEN']:
            self.send_error(403); return
        body = identity if self.path == '/internal/plugin/status' else {
            'pid': os.getpid(), 'instance_id': identity['instance_id'],
            'version': identity['package_version'], 'cwd': os.getcwd(),
            'request_id': self.headers.get('X-Request-ID')}
        raw=json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        if self.headers.get('X-Local-Token') != os.environ['BOXTEAM_RUNTIME_CONTROL_TOKEN']:
            self.send_error(403); return
        if self.path.endswith('/crash'): os._exit(17)
        if mode == 'bad-shutdown': self.send_error(500); return
        self.send_response(200); self.send_header('Content-Length','2'); self.end_headers(); self.wfile.write(b'{}')
        threading.Thread(target=server.shutdown).start()
server=ThreadingHTTPServer(('127.0.0.1',int(os.environ['BOXTEAM_PLUGIN_PORT'])), Handler)
server.serve_forever()
server.server_close()
'''

NODE_PLUGIN = '''
import http from 'node:http';
const identity={...JSON.parse(process.env.BOXTEAM_PLUGIN_INSTANCE),status:'ready',process_id:process.pid,
  capabilities:['status','shutdown'],log_sink:JSON.parse(process.env.BOXTEAM_RUNTIME_LOG_SINK)};
const server=http.createServer((req,res)=>{
  if(req.headers['x-local-token']!==process.env.BOXTEAM_RUNTIME_CONTROL_TOKEN){res.writeHead(403);res.end();return;}
  res.writeHead(200,{'Content-Type':'application/json'});
  if(req.url==='/internal/plugin/shutdown'){res.end('{}');server.close();return;}
  res.end(JSON.stringify(req.url==='/internal/plugin/status'?identity:{pid:process.pid,
    instance_id:identity.instance_id,version:identity.package_version,request_id:req.headers['x-request-id']}));
});
server.listen(Number(process.env.BOXTEAM_PLUGIN_PORT),'127.0.0.1');
'''


@pytest.fixture
def plugin_gateway(request: pytest.FixtureRequest, integration_workspace_root_path: str, unused_tcp_port: int
) -> Iterator[tuple[httpx.Client, TestRunContext]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    gateway = start_gateway_process(workspace_root=Path(integration_workspace_root_path), default_backend_url=None,
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

    def bind(entries: list[dict[str, object]]) -> dict[str, object]:
        path = "/api/gateway/config/sources/gateway_mutable_override"
        document = exchange("GET", path).raise_for_status().json()["data"]
        source = commentjson.loads(document["jsonc"])
        source.setdefault("plugins", {})["runtime"] = {"entries": entries}
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

    def package(version: str, mode: str = '') -> Path:
        path = context.artifacts_dir / f"probe-{version}.zip"
        manifest = {"manifest_version": 1, "package_id": "probe.tools", "version": version,
            "name": "真实受控插件", "extension_contract_version": 1, "plugins": [
                {"id": runtime, "name": runtime, "instance_scope": "gateway",
                 "requires": [{"extension_point": "services", "version": 1}],
                 "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]},
                 "runtime": runtime, "entrypoint": ["nested/custom/entry.py", mode] if runtime == "python" else ["another/plugin.mjs"],
                 "management_protocol_version": 1} for runtime in ("python", "node")] + [
                {"id": "declarations", "name": "无进程配置", "instance_scope": "gateway",
                 "requires": [{"extension_point": "configuration", "version": 1}],
                 "contributes": {"configuration": {"type": "object", "properties": {}}}}]}
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("plugin.jsonc", json.dumps(manifest))
            archive.writestr("nested/custom/entry.py", PYTHON_PLUGIN)
            archive.writestr("nested/custom/local_module.py", "VALUE = 1\n")
            archive.writestr("another/plugin.mjs", NODE_PLUGIN)
        return path

    def install(version: str, mode: str = '') -> None:
        path = context.artifacts_dir / f"probe-{version}.zip"
        if not path.exists():
            path = package(version, mode)
        result = exchange("POST", "/api/gateway/service-plugins/install", {**target, "path": str(path),
            "expected_digest": hashlib.sha256(path.read_bytes()).hexdigest()})
        result.raise_for_status()

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
        install("1.0.0")
        assert len(inventory()["packages"]) == 1
        original_content = installed_content()
        current = bind(entries())
        assert current["declarations"]["instance"] is None and current["declarations"]["declarative"] is True
        python, node = current["python"], current["node"]
        assert python["state"] == node["state"] == "ready"
        assert python["instance"]["process_id"] != node["instance"]["process_id"]
        assert echo(python).raise_for_status().json()["version"] == "1.0.0"
        assert echo(node).raise_for_status().json()["version"] == "1.0.0"
        assert "/plugins/packages/" not in echo(python).json()["cwd"]
        blocked = exchange("POST", "/api/gateway/service-plugins/uninstall",
            {**target, "package_id": "probe.tools", "package_version": "1.0.0"})
        assert blocked.status_code == 409
        restarted = exchange("POST", "/api/gateway/service-plugins/restart", {**target,
            "package_id": "probe.tools", "package_version": "1.0.0", "plugin_id": "python", "revision": python["revision"]})
        restarted.raise_for_status()
        python_new = restarted.json()["data"]
        assert python_new["instance"]["instance_id"] != python["instance"]["instance_id"]
        assert not psutil.pid_exists(python["instance"]["process_id"])
        assert echo(python).status_code == 409
        assert states()["node"]["instance"] == node["instance"]
        assert installed_content() == original_content, "运行、导入本地模块和重启不能修改已安装包"
        crash_path = (f"/api/gateway/service-plugins/probe.tools/python/instances/"
            f"{python_new['instance']['instance_id']}/services/echo/crash?package_version=1.0.0")
        assert client.post(crash_path).status_code == 500
        assert states()["python"]["state"] == "failed"
        assert not psutil.pid_exists(python_new["instance"]["process_id"])
        assert states()["python"]["binding"]["enabled"] is True
        assert echo(python_new).status_code == 409
        assert states()["node"]["instance"] == node["instance"]
        recovered = exchange("POST", "/api/gateway/service-plugins/restart", {**target,
            "package_id": "probe.tools", "package_version": "1.0.0", "plugin_id": "python",
            "revision": python_new["revision"]}).raise_for_status().json()["data"]
        assert recovered["state"] == "ready" and recovered["instance"] != python_new["instance"]
        install("2.0.0")
        assert states()["python"]["binding"]["package_version"] == "1.0.0"
        switched = bind(entries("2.0.0"))
        assert echo(switched["python"]).raise_for_status().json()["version"] == "2.0.0"
        assert echo(python_new).status_code == 409
        disabled = bind(entries("2.0.0", python_enabled=False))
        assert disabled["python"]["state"] == "stopped"
        assert not psutil.pid_exists(switched["python"]["instance"]["process_id"])
        assert echo(switched["python"]).status_code == 409
        assert states()["python"]["binding"]["enabled"] is False
        install("3.0.0", "bad-ready")
        failed = bind(entries("3.0.0"))
        assert failed["python"]["state"] == "failed"
        assert failed["python"]["binding"]["enabled"] is True
        assert failed["node"]["state"] == "ready", "单个插件失败不能阻断兄弟启动"
        assert bind(entries("3.0.0"))["python"]["instance"] is None, "相同绑定读取或提交不得重试启动"
        install("4.0.0", "bad-shutdown")
        running = bind(entries("4.0.0"))["python"]
        assert running["state"] == "ready"
        stop_failed = bind(entries("4.0.0", python_enabled=False))["python"]
        assert stop_failed["state"] == "failed" and "shutdown" in stop_failed["error"]
        assert not psutil.pid_exists(running["instance"]["process_id"]), "管理响应失败也须核实实际进程退出"
        assert bind(entries("4.0.0", python_enabled=False))["python"]["state"] == "failed", "相同配置不能洗掉停止失败回执"
        assert bind([]) == {}, "解除全部绑定后不得继续展示幽灵插件"
        for version in ("1.0.0", "2.0.0", "3.0.0", "4.0.0"):
            exchange("POST", "/api/gateway/service-plugins/uninstall", {**target,
                "package_id": "probe.tools", "package_version": version}).raise_for_status()
        assert inventory()["packages"] == []
    finally:
        (context.artifacts_dir / "plugin-flow.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
