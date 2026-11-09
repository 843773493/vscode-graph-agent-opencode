"""真实 Gateway 插件包安装、依赖导入、调用和重启。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.plugin_package import native_plugin_target
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


def _dependency_wheel() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("plugin_dependency/__init__.py", "VALUE = 'locked-dependency-ready'\n")
        archive.writestr("plugin_dependency-1.0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: plugin-dependency\nVersion: 1.0.0\n\n")
        archive.writestr("plugin_dependency-1.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("plugin_dependency-1.0.0.dist-info/RECORD", "")
    return output.getvalue()


def _plugin_source() -> str:
    return '''
import json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from plugin_dependency import VALUE
identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])
identity.update(status='ready', process_id=os.getpid(), capabilities=['status','shutdown'],
    log_sink=json.loads(os.environ['BOXTEAM_RUNTIME_LOG_SINK']))
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('X-Local-Token') != os.environ['BOXTEAM_RUNTIME_CONTROL_TOKEN']:
            self.send_error(403); return
        body = identity if self.path == '/internal/plugin/status' else {
            'value': VALUE, 'instance_id': identity['instance_id'],
            'request_id': self.headers.get('X-Request-ID')}
        raw = json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        if self.headers.get('X-Local-Token') != os.environ['BOXTEAM_RUNTIME_CONTROL_TOKEN']:
            self.send_error(403); return
        self.send_response(200); self.send_header('Content-Length','2'); self.end_headers(); self.wfile.write(b'{}')
        threading.Thread(target=server.shutdown).start()
server = ThreadingHTTPServer(('127.0.0.1', int(os.environ['BOXTEAM_PLUGIN_PORT'])), Handler)
server.serve_forever(); server.server_close()
'''


def _python_lock(dependency_version: str = "1.0.0") -> str:
    lines = [
        "version = 1",
        "revision = 3",
        f'requires-python = "=={sys.version_info.major}.{sys.version_info.minor}.*"',
        "",
        "[[package]]",
        'name = "plugin-dependency"',
        f'version = "{dependency_version}"',
        'source = { registry = "https://example.invalid/simple" }',
        "",
        "[[package]]",
        'name = "plugin-dependency-fixture"',
        'version = "1.0.0"',
        'source = { virtual = "." }',
        "",
        "[package.metadata]",
        f'requires-dist = [{{ name = "plugin-dependency", specifier = "=={dependency_version}" }}]',
    ]
    return "\n".join(lines) + "\n"


def _plugin_archive(path: Path, *, package_version: str, python_version: str = "1.0.0",
    entry_content: str | None = None, include_dependency: bool = True,
    lock_dependency_version: str | None = None) -> Path:
    manifest = {
        "manifest_version": 1,
        "package_id": "dependency.probe",
        "version": package_version,
        "name": "依赖准备真实入口探针",
        "extension_contract_version": 1,
        "plugins": [{
            "id": "probe",
            "name": "锁定依赖探针",
            "instance_scope": "gateway",
            "requires": [{"extension_point": "services", "version": 1}],
            "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]},
            "runtime": "python",
            "entrypoint": ["entry.py"],
            "management_protocol_version": 1,
        }],
        "target": native_plugin_target("python"),
    }
    project = (
        "[project]\nname = 'plugin-dependency-fixture'\nversion = '1.0.0'\n"
        f"requires-python = '=={sys.version_info.major}.{sys.version_info.minor}.*'\n"
        f"dependencies = ['plugin-dependency=={python_version}']\n"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("pyproject.toml", project)
        archive.writestr("uv.lock", _python_lock(lock_dependency_version or python_version))
        archive.writestr("entry.py", entry_content or _plugin_source())
        if include_dependency:
            with zipfile.ZipFile(BytesIO(_dependency_wheel())) as wheel:
                for entry in wheel.infolist():
                    archive.writestr(f"python/site-packages/{entry.filename}", wheel.read(entry.filename))
    return path


def _gateway_path_without_uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        return os.environ.get("PATH", "")
    uv_path = Path(uv).resolve()
    entries = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            entries.append(entry)
            continue
        directory = Path(entry)
        if any(
            candidate.exists() and candidate.resolve() == uv_path
            for candidate in (directory / "uv", directory / "uv.exe")
        ):
            continue
        entries.append(entry)
    path = os.pathsep.join(entries)
    if shutil.which("uv", path=path) is not None:
        raise RuntimeError("无法从 Gateway 测试 PATH 中移除 uv")
    return path


@pytest.fixture
def dependency_plugin_gateway(request: pytest.FixtureRequest, integration_workspace_root_path: str,
    unused_tcp_port: int):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir
    packages = {
        "good": _plugin_archive(artifacts / "dependency-plugin.zip", package_version="1.0.0"),
        "stale": _plugin_archive(
            artifacts / "dependency-plugin-stale.zip", package_version="2.0.0",
            python_version="2.0.0", lock_dependency_version="1.0.0",
        ),
        "missing": _plugin_archive(
            artifacts / "dependency-plugin-missing.zip", package_version="3.0.0", include_dependency=False,
        ),
        "changed": _plugin_archive(
            artifacts / "dependency-plugin-changed.zip", package_version="1.0.0",
            entry_content=_plugin_source() + "\n# changed package contents\n",
        ),
    }
    template = Path.cwd() / "tests/fixtures/workspaces/default_test_workspace"
    config = Path.cwd() / "configs/tests/workspace/default.jsonc"
    schema = Path.cwd() / "configs/workspace_schema.jsonc"
    gateway_workspace = prepare_test_workspace(
        workspace_root=Path(integration_workspace_root_path), template_root=template, template_items=(),
    )
    install_test_workspace_config(workspace_root=gateway_workspace, config_path=config, schema_path=schema)
    gateway_url = f"http://127.0.0.1:{unused_tcp_port}"
    config_path = gateway_workspace / ".boxteam/workspace.jsonc"
    payload = commentjson.loads(config_path.read_text(encoding="utf-8"))
    payload.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("connection", {})["url"] = gateway_url
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    gateway = start_gateway_process(
        workspace_root=gateway_workspace,
        default_backend_url=None,
        port=unused_tcp_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir,
        extra_env={"PATH": _gateway_path_without_uv()},
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=30, trust_env=False) as client:
            client.post("/api/gateway/users/guest", json={}).raise_for_status()
            active = client.get("/api/gateway/workspaces").raise_for_status().json()["data"]["active_workspace"]
            client.headers.update({
                "X-BoxTeam-Target-Gateway-Id": active["gateway_id"],
                "X-BoxTeam-Gateway-Id": active["gateway_id"],
                "X-BoxTeam-Workspace-Id": active["workspace_id"],
            })
            client.get("/api/v1/health").raise_for_status()
            yield client, context, packages
    finally:
        close_gateway_process(gateway)



def test_gateway_installs_runs_and_restarts_prepared_dependency_package(
    dependency_plugin_gateway,
) -> None:
    client, context, packages = dependency_plugin_gateway
    evidence: list[dict[str, object]] = []
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]

    def exchange(method: str, path: str, body: dict[str, object] | None = None) -> httpx.Response:
        request_id = f"plugin-dependency-{len(evidence) + 1}"
        response = client.request(method, path, json=body, headers={"X-Request-ID": request_id})
        assert response.headers.get("X-Request-ID") == request_id
        if response.headers.get("content-type", "").startswith("application/json"):
            payload = response.json()
            if "request_id" in payload:
                assert payload["request_id"] == request_id
        evidence.append({
            "method": method,
            "path": path,
            "request_id": request_id,
            "status": response.status_code,
            "request": body,
            "response": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text,
        })
        return response

    def install(archive: Path) -> httpx.Response:
        return exchange("POST", "/api/gateway/service-plugins/install", {
            "gateway_id": gateway_id,
            "path": str(archive),
            "expected_digest": hashlib.sha256(archive.read_bytes()).hexdigest(),
        })

    def status() -> dict[str, object]:
        response = exchange("POST", "/api/gateway/service-plugins/status", {"gateway_id": gateway_id})
        return response.raise_for_status().json()["data"]

    def bind() -> dict[str, object]:
        path = "/api/gateway/config/sources/gateway_mutable_override"
        source = exchange("GET", path).raise_for_status().json()["data"]
        payload = commentjson.loads(source["jsonc"])
        payload.setdefault("plugins", {})["runtime"] = {"entries": [{
            "package_id": "dependency.probe",
            "package_version": "1.0.0",
            "plugin_id": "probe",
            "enabled": True,
        }]}
        response = exchange("PUT", path, {
            "gateway_id": gateway_id,
            "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(payload, ensure_ascii=False, indent=2),
            "base_file_digest": source["file_digest"],
            "base_layer_revision": source["layer_revision"],
            "base_layer_digest": source["layer_digest"],
            "expected_active_revision": source["active_revision"],
            "expected_active_digest": source["active_digest"],
            "idempotency_key": "plugin-dependency-first-bind",
        }).raise_for_status().json()["data"]
        assert response["edit"]["state"] == "active"
        return next(item for item in status()["plugins"] if item["binding"]["plugin_id"] == "probe")

    def call(instance: dict[str, object]) -> httpx.Response:
        slot = instance["instance"]
        path = (
            f"/api/gateway/service-plugins/dependency.probe/probe/instances/{slot['instance_id']}"
            "/services/echo/value?package_version=1.0.0"
        )
        return exchange("GET", path)

    try:
        assert install(packages["good"]).status_code == 200
        stale = install(packages["stale"])
        assert stale.status_code == 422
        assert "pyproject.toml 与 uv.lock 根依赖不一致" in stale.json()["detail"]
        missing = install(packages["missing"])
        assert missing.status_code == 422
        assert "缺少已准备的 python/site-packages" in missing.json()["detail"]

        inventory = exchange(
            "POST", "/api/gateway/service-plugins/inventory", {"gateway_id": gateway_id},
        ).raise_for_status().json()["data"]["packages"]
        assert {item["manifest"]["version"] for item in inventory} == {"1.0.0"}

        ready = bind()
        assert ready["state"] == "ready"
        reply = call(ready).raise_for_status().json()
        assert reply["value"] == "locked-dependency-ready"
        assert reply["instance_id"] == ready["instance"]["instance_id"]

        restarted = exchange("POST", "/api/gateway/service-plugins/restart", {
            "gateway_id": gateway_id,
            "package_id": "dependency.probe",
            "package_version": "1.0.0",
            "plugin_id": "probe",
            "revision": ready["revision"],
        }).raise_for_status().json()["data"]
        assert restarted["instance"]["instance_id"] != ready["instance"]["instance_id"]
        assert call(restarted).raise_for_status().json()["value"] == "locked-dependency-ready"
        assert install(packages["changed"]).status_code == 409, "同包版本内容不可变"
        assert call(restarted).raise_for_status().json()["value"] == "locked-dependency-ready"
    finally:
        (context.artifacts_dir / "plugin-dependency-flow.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
