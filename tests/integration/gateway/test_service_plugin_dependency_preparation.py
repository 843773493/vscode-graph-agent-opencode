"""真实 Gateway 插件包安装、依赖导入、调用和重启。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.http_server import running_http_server
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


def _plugin_archive(path: Path, *, package_version: str, lock: str, python_version: str = "1.0.0",
    entry_content: str | None = None) -> Path:
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
    }
    project = (
        "[project]\nname = 'plugin-dependency-fixture'\nversion = '1.0.0'\n"
        f"requires-python = '=={sys.version_info.major}.{sys.version_info.minor}.*'\n"
        f"dependencies = ['plugin-dependency=={python_version}']\n"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("pyproject.toml", project)
        archive.writestr("uv.lock", lock)
        archive.writestr("entry.py", entry_content or _plugin_source())
    return path


@pytest.fixture
def dependency_plugin_gateway(request: pytest.FixtureRequest, integration_workspace_root_path: str,
    unused_tcp_port: int):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
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
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=30, trust_env=False) as client:
            yield client, context
    finally:
        close_gateway_process(gateway)


def test_gateway_installs_runs_and_restarts_locked_dependency_package(dependency_plugin_gateway,
    request: pytest.FixtureRequest) -> None:
    client, context = dependency_plugin_gateway
    registry_root = context.artifacts_dir / "registry"
    package_page = registry_root / "simple/plugin-dependency"
    package_page.mkdir(parents=True)
    (registry_root / "plugin_dependency-1.0.0-py3-none-any.whl").write_bytes(_dependency_wheel())
    (package_page / "index.html").write_text(
        '<a href="../../plugin_dependency-1.0.0-py3-none-any.whl">plugin-dependency</a>', encoding="utf-8",
    )
    uv = shutil.which("uv")
    assert uv is not None
    environment = {key: value for key, value in os.environ.items()
        if not key.upper().startswith(("UV_", "PIP_", "PYTHON"))
        and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}}
    evidence: list[dict[str, object]] = []
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    package_dir = context.artifacts_dir / "bundle"
    package_dir.mkdir()
    (package_dir / "pyproject.toml").write_text(
        "[project]\nname = 'plugin-dependency-fixture'\nversion = '1.0.0'\n"
        f"requires-python = '=={sys.version_info.major}.{sys.version_info.minor}.*'\n"
        "dependencies = ['plugin-dependency==1.0.0']\n", encoding="utf-8",
    )

    with running_http_server(ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(registry_root)),
    )) as registry:
        registry_url = f"http://127.0.0.1:{registry.server_port}/simple"
        lock_command = [
            uv, "--no-config", "--project", str(package_dir), "lock", "--python", sys.executable,
            "--no-python-downloads", "--no-managed-python", "--default-index", registry_url,
        ]
        lock_result = subprocess.run(
            lock_command, cwd=package_dir, env=environment, capture_output=True, text=True, timeout=45, check=False,
        )
        (context.artifacts_dir / "uv-lock-command.json").write_text(json.dumps({
            "command": lock_command,
            "returncode": lock_result.returncode,
            "stdout": lock_result.stdout,
            "stderr": lock_result.stderr,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if lock_result.returncode:
            raise AssertionError(f"本地锁文件准备失败: exit={lock_result.returncode}: {lock_result.stderr}")
        lock_text = (package_dir / "uv.lock").read_text(encoding="utf-8")
        good = _plugin_archive(context.artifacts_dir / "dependency-plugin.zip",
            package_version="1.0.0", lock=lock_text)
        stale = _plugin_archive(context.artifacts_dir / "dependency-plugin-stale.zip",
            package_version="2.0.0", lock=lock_text, python_version="2.0.0")
        changed = _plugin_archive(context.artifacts_dir / "dependency-plugin-changed.zip",
            package_version="1.0.0", lock=lock_text,
            entry_content=_plugin_source() + "\n# changed package contents\n")

        counter = 0

        def exchange(method: str, path: str, body: dict[str, object] | None = None) -> httpx.Response:
            nonlocal counter
            counter += 1
            request_id = f"plugin-dependency-{counter}"
            response = client.request(method, path, json=body, headers={"X-Request-ID": request_id})
            assert response.headers.get("X-Request-ID") == request_id
            if response.headers.get("content-type", "").startswith("application/json"):
                payload = response.json()
                if "request_id" in payload:
                    assert payload["request_id"] == request_id
            evidence.append({"method": method, "path": path, "request_id": request_id,
                "status": response.status_code, "request": body,
                "response": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text})
            return response

        def install(archive: Path) -> httpx.Response:
            return exchange("POST", "/api/gateway/service-plugins/install", {
                "gateway_id": gateway_id,
                "path": str(archive),
                "expected_digest": hashlib.sha256(archive.read_bytes()).hexdigest(),
            })

        def status() -> dict[str, object]:
            return exchange("POST", "/api/gateway/service-plugins/status", {"gateway_id": gateway_id})\
                .raise_for_status().json()["data"]

        def bind() -> dict[str, object]:
            path = "/api/gateway/config/sources/gateway_mutable_override"
            source = exchange("GET", path).raise_for_status().json()["data"]
            payload = commentjson.loads(source["jsonc"])
            payload.setdefault("plugins", {})["runtime"] = {"entries": [{
                "package_id": "dependency.probe", "package_version": "1.0.0", "plugin_id": "probe", "enabled": True,
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
            return exchange("GET", f"/api/gateway/service-plugins/dependency.probe/probe/instances/"
                f"{slot['instance_id']}/services/echo/value?package_version=1.0.0")

        try:
            assert install(good).status_code == 200
            stale_response = install(stale)
            assert stale_response.status_code == 409
            assert "stage=python-sync" in stale_response.json()["detail"]
            assert not any(item["manifest"]["version"] == "2.0.0"
                for item in exchange("POST", "/api/gateway/service-plugins/inventory", {"gateway_id": gateway_id})
                    .raise_for_status().json()["data"]["packages"])
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
            assert install(changed).status_code == 409, "同包版本内容不可变"
            assert call(restarted).raise_for_status().json()["value"] == "locked-dependency-ready"
        finally:
            (context.artifacts_dir / "plugin-dependency-flow.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
