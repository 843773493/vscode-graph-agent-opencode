"""Linux Gateway A 经真实 OpenSSH 管理 Windows Gateway B 的插件完整生命周期。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import commentjson
import httpx
import pytest
from pydantic import JsonValue, TypeAdapter

from app.gateway.runtime.ssh_process import start_ssh_tunnel_process
from app.gateway.service_types import LocalForwardSpec
from app.gateway.ssh_command import build_ssh_command
from app.schemas.gateway.plugin_installation import (
    PluginInstallOperationDTO,
    PluginSourceArtifact,
    PluginSourceCatalogDTO,
)
from app.schemas.gateway.service_plugin import PluginRuntimeStatusDTO
from configs.installer import install_user_configuration
from tests.harness.python.run_context import TestRunContext
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)

_REMOTE_ALIAS = os.environ.get("BOXTEAM_REMOTE_PLUGIN_GATEWAY_ALIAS")
_REMOTE_HOME = os.environ.get("BOXTEAM_REMOTE_PLUGIN_GATEWAY_HOME")
_REMOTE_REPOSITORY = os.environ.get("BOXTEAM_REMOTE_PLUGIN_GATEWAY_REPOSITORY")
_REMOTE_PYTHON = os.environ.get("BOXTEAM_REMOTE_PLUGIN_GATEWAY_PYTHON")
_REMOTE_PORT = int(os.environ.get("BOXTEAM_REMOTE_PLUGIN_GATEWAY_PORT", "8014"))
_PACKAGE_ID = "remote.ssh.lifecycle"
_PLUGIN_ID = "http_probe"
_WINDOWS_SERVER_PORT = 18414
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class _PluginCatalog:
    def __init__(self, bind_host: str, bind_port: int, contents: dict[str, bytes]) -> None:
        self.contents = contents
        self.targets = {
            version: json.loads(zipfile.ZipFile(io.BytesIO(content)).read("plugin.jsonc"))["target"]
            for version, content in contents.items()
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == "/catalog":
                    items = [{
                        "package_id": _PACKAGE_ID,
                        "package_version": version,
                        "target": owner.targets[version],
                        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                        "url": f"http://{bind_host}:{owner.server.server_port}/plugin-{version}.zip",
                        "filename": f"{_PACKAGE_ID}-{version}.zip",
                    } for version, content in owner.contents.items()]
                    body = json.dumps({
                        "protocol_version": 1,
                        "source_id": "remote-ssh-proof",
                        "catalog_revision": "remote-ssh-catalog-v1",
                        "items": items,
                        "next_cursor": None,
                    }).encode()
                    content_type = "application/json"
                elif path.startswith("/plugin-"):
                    version = path.removeprefix("/plugin-").removesuffix(".zip")
                    body = owner.contents.get(version)
                    if body is None:
                        self.send_error(404)
                        return
                    content_type = "application/zip"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer((bind_host, bind_port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_port

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _plugin_archive(version: str) -> bytes:
    manifest = {
        "manifest_version": 1,
        "package_id": _PACKAGE_ID,
        "version": version,
        "name": "Remote SSH lifecycle probe",
        "target": {
            "os": "windows",
            "architecture": "x64",
            "system_baseline": "windows-10.0.0",
            "python": {"version": "3.12.4", "abi": "cp312"},
        },
        "extension_contract_version": 1,
        "plugins": [{
            "id": _PLUGIN_ID,
            "name": "Remote HTTP probe",
            "instance_scope": "gateway",
            "runtime": "python",
            "entrypoint": ["server.py"],
            "management_protocol_version": 1,
            "requires": [{"extension_point": "services", "version": 1}],
            "contributes": {"services": [{
                "service_id": "echo", "service_type": "terminal", "protocol": "http",
            }]},
        }],
    }
    source = '''
import json, os, sys
sys.path.insert(0, os.environ['BOXTEAM_PROJECT_ROOT'])
from fastapi import FastAPI, Request
from app.core.logging_config import configure_application_logging
from app.runtime.plugin_commands import plugin_command_router
import uvicorn
configure_application_logging(service='remote-lifecycle-probe')
identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])
app = FastAPI()
async def shutdown(): server.should_exit = True
app.include_router(plugin_command_router({}, shutdown=shutdown))
@app.get('/services/echo/')
async def echo(request: Request):
    return {'instance_id': identity['instance_id'], 'package_version': identity['package_version'],
            'process_id': os.getpid(), 'request_id': request.headers.get('X-Request-ID')}
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1',
    port=int(os.environ['BOXTEAM_PLUGIN_PORT']), log_config=None))
server.run()
'''
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("server.py", source)
    return content.getvalue()


def _ssh_output(*, alias: str, remote_command: str) -> str:
    from app.gateway.ssh_config import resolve_user_ssh_host

    host = resolve_user_ssh_host(alias)
    result = subprocess.run(
        build_ssh_command(
            host=host.hostname,
            port=host.port,
            username=host.username,
            private_key_path=None,
            ssh_config_host=alias,
            remote_command=remote_command,
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"远端 SSH 查询失败: exit={result.returncode}, {result.stderr[-1000:]}")
    return result.stdout.strip()


def _remote_process_alive(alias: str, pid: int) -> bool:
    script = f"[string]($null -ne (Get-Process -Id {pid} -ErrorAction SilentlyContinue))"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    output = _ssh_output(alias=alias, remote_command=f"powershell.exe -NoProfile -EncodedCommand {encoded}")
    values = [line.strip() for line in output.splitlines() if line.strip().lower() in {"true", "false"}]
    if not values:
        raise RuntimeError("远端进程检查没有返回布尔结果")
    return values[-1].lower() == "true"


def _remote_path_exists(alias: str, path: str) -> bool:
    escaped = path.replace("'", "''")
    script = f"[string](Test-Path -LiteralPath '{escaped}')"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    output = _ssh_output(alias=alias, remote_command=f"powershell.exe -NoProfile -EncodedCommand {encoded}")
    values = [line.strip() for line in output.splitlines() if line.strip().lower() in {"true", "false"}]
    if not values:
        raise RuntimeError("远端文件存在检查没有返回布尔结果")
    return values[-1].lower() == "true"


def _remote_file_contents(alias: str, path: str) -> str:
    escaped_path = path.replace("'", "''")
    script = f"Get-Content -Raw -LiteralPath '{escaped_path}'"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return _ssh_output(
        alias=alias,
        remote_command=f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}",
    )


@pytest.mark.windows_gateway_server
def test_serve_isolated_windows_gateway_for_remote_lifecycle_acceptance(
    integration_workspace_root_path: str,
) -> None:
    """专用 Windows target test owner；由 Linux 验收完成后通过 task Home sentinel 关闭。"""
    if sys.platform != "win32":
        pytest.skip("只在配置了专用 server Home 的 Windows 目标运行")
    home_value = os.environ.get("BOXTEAM_HOME")
    if not home_value:
        pytest.skip("没有隔离 Windows Gateway server Home")
    home = Path(home_value)
    stop_path = home / ".remote-plugin-server-stop"
    ready_path = home / ".remote-plugin-server-ready.json"
    if not (home.parent / ".remote-plugin-server-mode").is_file():
        pytest.skip(f"没有显式启用一次性 Windows Gateway server test: home={home}")
    stop_path.unlink(missing_ok=True)
    ready_path.unlink(missing_ok=True)
    workspace = Path(integration_workspace_root_path)
    prepare_default_test_workspace(
        workspace_root=workspace,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    install_test_workspace_config(
        workspace_root=workspace,
        config_path=Path("configs/tests/workspace/default.jsonc"),
        schema_path=Path("configs/workspace_schema.jsonc"),
    )
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    from tests.support.e2e_control.harness import E2EProcessLease

    lease = E2EProcessLease(
        context.runtime_root_for_node("windows-gateway-b-server") / "ports.json",
        (_WINDOWS_SERVER_PORT,),
    )
    lease.acquire()
    gateway = None
    try:
        gateway = start_gateway_process(
            workspace_root=workspace,
            default_backend_url=None,
            port=_WINDOWS_SERVER_PORT,
            boxteam_home=home,
            artifacts_dir=context.artifacts_dir / "windows-gateway-b",
        )
        gateway_id = httpx.get(
            f"http://127.0.0.1:{_WINDOWS_SERVER_PORT}/api/gateway/health",
            headers=LOCAL_TOKEN_HEADERS,
            timeout=10,
            trust_env=False,
        ).raise_for_status().json()["data"]["gateway_id"]
        ready_path.write_text(json.dumps({
            "gateway_id": gateway_id,
            "process_id": gateway.process.pid,
            "port": _WINDOWS_SERVER_PORT,
        }), encoding="utf-8")
        deadline = time.monotonic() + 480
        while not stop_path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Linux Gateway federation acceptance did not close the Windows Gateway server")
            time.sleep(0.5)
    finally:
        if gateway is not None:
            close_gateway_process(gateway)
        ready_path.unlink(missing_ok=True)
        lease.release()


@pytest.fixture
def remote_gateway_clients(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
) -> Iterator[tuple[httpx.Client, httpx.Client, TestRunContext, str, str, str, int]]:
    if not all((_REMOTE_ALIAS, _REMOTE_HOME, _REMOTE_REPOSITORY, _REMOTE_PYTHON)):
        pytest.skip("需要显式配置已启动的隔离 Windows Gateway SSH 目标")
    assert _REMOTE_ALIAS is not None
    assert _REMOTE_HOME is not None
    assert _REMOTE_REPOSITORY is not None
    assert _REMOTE_PYTHON is not None
    remote_alias = _REMOTE_ALIAS
    remote_home = _REMOTE_HOME
    remote_repository = _REMOTE_REPOSITORY
    remote_python = _REMOTE_PYTHON
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = Path(integration_workspace_root_path)
    prepare_default_test_workspace(
        workspace_root=workspace,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    install_test_workspace_config(
        workspace_root=workspace,
        config_path=Path("configs/tests/workspace/default.jsonc"),
        schema_path=Path("configs/workspace_schema.jsonc"),
    )
    home = context.boxteam_home_for_node(request.node.nodeid)
    install_user_configuration(config_root=home / "config", profile="default", project_root=Path.cwd())
    gateway_config_path = home / "config/gateway.jsonc"
    gateway_config = commentjson.loads(gateway_config_path.read_text(encoding="utf-8"))
    from app.gateway.ssh_config import resolve_user_ssh_host

    remote = resolve_user_ssh_host(remote_alias)
    escaped_remote_home = remote_home.replace("'", "''")
    escaped_remote_repository = remote_repository.replace("'", "''")
    escaped_remote_python = remote_python.replace("'", "''")
    remote_command = (
        "powershell.exe -NoLogo -NoProfile -NonInteractive -Command \""
        f"$env:BOXTEAM_HOME='{escaped_remote_home}'; "
        f"$env:BOXTEAM_GATEWAY_ROOT='{escaped_remote_home}/state/gateway'; "
        f"Set-Location '{escaped_remote_repository}'; "
        f"& '{escaped_remote_python}' -m app.gateway.federation_pairing {{pairing_args}}\""
    )
    gateway_config["workspaces"] = [{
        "kind": "remote_gateway",
        "connection_id": "remote_plugin_ssh_lifecycle",
        "name": "Isolated Windows Gateway",
        "host": remote.hostname,
        "port": remote.port,
        "username": remote.username,
        "ssh_config_host": remote_alias,
        "remote_pair_command": remote_command,
        "remote_gateway_port": _REMOTE_PORT,
        "activate": False,
    }]
    from tests.support.e2e_control.harness import E2EProcessLease
    from tests.support.ports import integration_port_block_for_file

    ports = integration_port_block_for_file(Path(request.node.path))
    lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "ports.json",
                            (ports.port(0), ports.port(1), ports.port(2)))
    lease.acquire()
    gateway_config_path.write_text(json.dumps(gateway_config, ensure_ascii=False, indent=2), encoding="utf-8")
    gateway = None
    tunnel = None
    client_a = None
    client_b = None
    try:
        gateway = start_gateway_process(
            workspace_root=workspace,
            default_backend_url=None,
            port=ports.port(0),
            boxteam_home=home,
            artifacts_dir=context.artifacts_dir / "gateway-a",
        )
        with socket.create_connection((remote.hostname, remote.port), timeout=5) as route_probe:
            local_route_address = route_probe.getsockname()[0]
        tunnel = start_ssh_tunnel_process(
            host=remote.hostname,
            port=remote.port,
            username=remote.username,
            private_key_path=None,
        ssh_config_host=remote_alias,
            forwards=(LocalForwardSpec(
                name="workspace_api", local_port=ports.port(1),
                remote_host="127.0.0.1", remote_port=_REMOTE_PORT,
            ),),
            log_dir=context.artifacts_dir / "gateway-b-direct-ssh",
        )
        token_path = remote_home.rstrip("/\\") + "/state/gateway/credentials/local-token"
        token = _remote_file_contents(alias=remote_alias, path=token_path).splitlines()[-1].strip()
        if not token:
            raise RuntimeError("隔离 Windows Gateway 缺少本机认证令牌")
        client_a = httpx.Client(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=30, trust_env=False,
        )
        client_b = httpx.Client(
            base_url=f"http://127.0.0.1:{ports.port(1)}", headers={"X-Local-Token": token},
            timeout=30, trust_env=False,
        )
        health: httpx.Response | None = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            health = client_a.get("/api/gateway/health")
            if health.status_code == 200:
                break
            time.sleep(0.1)
        assert health is not None
        health.raise_for_status()
        gateway_b_health = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            gateway_b_health = client_b.get("/api/gateway/health")
            if gateway_b_health.status_code == 200:
                break
            time.sleep(0.1)
        assert gateway_b_health is not None
        gateway_b_health.raise_for_status()
        yield client_a, client_b, context, str(health.json()["data"]["gateway_id"]), str(
            gateway_b_health.json()["data"]["gateway_id"]
        ), local_route_address, ports.port(2)
    finally:
        if client_b is not None:
            client_b.close()
        if client_a is not None:
            client_a.close()
        if tunnel is not None:
            tunnel.close()
        if gateway is not None:
            close_gateway_process(gateway)
        lease.release()


def test_real_ssh_remote_plugin_install_update_switch_unbind_and_revoke(
    remote_gateway_clients: tuple[httpx.Client, httpx.Client, TestRunContext, str, str, str, int],
) -> None:
    client_a, client_b, context, gateway_a, gateway_b, local_ip, source_port = remote_gateway_clients
    assert _REMOTE_ALIAS is not None and _REMOTE_HOME is not None
    remote_alias = _REMOTE_ALIAS
    remote_home = _REMOTE_HOME
    catalog = _PluginCatalog(local_ip, source_port, {version: _plugin_archive(version) for version in ("1.0.0", "1.1.0")})
    evidence: list[dict[str, object]] = []
    connection_id: str | None = None

    def record(response: httpx.Response, request_id: str) -> httpx.Response:
        assert response.headers["X-Request-ID"] == request_id, response.text
        if response.headers.get("content-type", "").startswith("application/json"):
            assert response.json().get("request_id") == request_id, response.text
        evidence.append({"request_id": request_id, "status": response.status_code, "path": response.request.url.path})
        return response

    def direct(
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        client: httpx.Client = client_b,
    ) -> httpx.Response:
        request_id = f"remote-plugin-direct-{len(evidence)}"
        return record(client.request(method, path, json=body, headers={"X-Request-ID": request_id}), request_id)

    def relay(method: str, path: str, body: dict[str, object] | None = None, *, expected_status: int = 200) -> httpx.Response:
        request_id = f"remote-plugin-relay-{len(evidence)}"
        response = client_a.request(method, path, json=body, headers={
            "X-Request-ID": request_id,
            "X-BoxTeam-Target-Gateway-Id": gateway_b,
            "X-BoxTeam-Gateway-Connection-Id": connection_id or "",
        })
        record(response, request_id)
        assert response.status_code == expected_status, response.text
        return response

    def edit_b(update: Callable[[dict[str, JsonValue]], None]) -> dict[str, JsonValue]:
        source_path = "/api/gateway/config/sources/gateway_mutable_override"
        before = direct("GET", source_path).raise_for_status().json()["data"]
        document = _JSON_OBJECT_ADAPTER.validate_python(commentjson.loads(before["jsonc"]))
        update(document)
        response = direct("PUT", source_path, {
            "gateway_id": gateway_b,
            "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(document, ensure_ascii=False),
            "base_file_digest": before["file_digest"],
            "base_layer_revision": before["layer_revision"],
            "base_layer_digest": before["layer_digest"],
            "expected_active_revision": before["active_revision"],
            "expected_active_digest": before["active_digest"],
            "idempotency_key": f"remote-plugin-{time.monotonic_ns()}",
        }).raise_for_status()
        result: dict[str, JsonValue] = _JSON_OBJECT_ADAPTER.validate_python(response.json()["data"])
        edit = _JSON_OBJECT_ADAPTER.validate_python(result["edit"])
        assert edit["state"] == "active", result
        return result

    def publish_policy(capabilities: set[str]) -> None:
        from app.gateway.federation.ports import principal_ref_for_gateway

        def update(document: dict[str, JsonValue]) -> None:
            permissions = _JSON_OBJECT_ADAPTER.validate_python(document.get("permissions", {}))
            permissions["federation"] = {
                "default_effect": "deny",
                "rules": [{
                    "capability": capability, "effect": "allow",
                    "principal_ref": principal_ref_for_gateway(gateway_a),
                } for capability in sorted(capabilities)],
            }
            document["permissions"] = permissions
        edit_b(update)

    def install(artifact: PluginSourceArtifact, *, expected_status: str = "succeeded") -> PluginInstallOperationDTO:
        operation_id = f"remote-ssh-install-{uuid4().hex}"
        accepted = PluginInstallOperationDTO.model_validate(relay("POST", "/api/gateway/service-plugins/install", {
            "gateway_id": gateway_b,
            "source_id": "remote-ssh-proof",
            "catalog_revision": "remote-ssh-catalog-v1",
            "artifact": artifact.model_dump(mode="json"),
            "operation_id": operation_id,
        }).raise_for_status().json()["data"])
        assert accepted.status in {"pending", "running"}
        deadline = time.monotonic() + 45
        current = accepted
        while current.status in {"pending", "running"}:
            assert time.monotonic() < deadline, current
            time.sleep(0.1)
            current = PluginInstallOperationDTO.model_validate(relay(
                "POST", "/api/gateway/service-plugins/install-operation", {
                    "gateway_id": gateway_b, "operation_id": operation_id,
                },
            ).raise_for_status().json()["data"])
        assert current.status == expected_status, current
        return current

    def parse_status(response: httpx.Response) -> PluginRuntimeStatusDTO:
        return PluginRuntimeStatusDTO.model_validate(response.raise_for_status().json()["data"])

    def direct_status(client: httpx.Client) -> PluginRuntimeStatusDTO:
        body = {"gateway_id": gateway_b}
        return parse_status(direct("POST", "/api/gateway/service-plugins/status", body, client=client))

    def relayed_status() -> PluginRuntimeStatusDTO:
        return parse_status(relay("POST", "/api/gateway/service-plugins/status", {"gateway_id": gateway_b}))

    def bind(version: str, *, enabled: bool = True) -> None:
        def update(document: dict[str, JsonValue]) -> None:
            plugins = _JSON_OBJECT_ADAPTER.validate_python(document.get("plugins", {}))
            plugins["runtime"] = {"entries": [{
                "package_id": _PACKAGE_ID,
                "plugin_id": _PLUGIN_ID,
                "package_version": version,
                "enabled": enabled,
            }]}
            document["plugins"] = plugins
        edit_b(update)

    def echo(client: httpx.Client, *, remote: bool, instance_id: str, version: str) -> dict[str, object]:
        path = (f"/api/gateway/service-plugins/{_PACKAGE_ID}/{_PLUGIN_ID}/instances/"
                f"{instance_id}/services/echo/?package_version={version}")
        request_id = f"remote-plugin-call-{len(evidence)}"
        if remote:
            response = client.get(path, headers={
                "X-Request-ID": request_id,
                "X-BoxTeam-Target-Gateway-Id": gateway_b,
                "X-BoxTeam-Gateway-Connection-Id": connection_id or "",
            })
        else:
            response = client.get(path, headers={"X-Request-ID": request_id})
        record(response, request_id).raise_for_status()
        result = response.json()
        assert result["instance_id"] == instance_id and result["request_id"] == request_id, result
        return result

    def installation_inventory(client: httpx.Client, remote: bool) -> list[dict[str, JsonValue]]:
        if remote:
            response = relay("POST", "/api/gateway/service-plugins/inventory", {"gateway_id": gateway_b})
        else:
            gateway_id = gateway_a if client is client_a else gateway_b
            response = direct(
                "POST", "/api/gateway/service-plugins/inventory", {"gateway_id": gateway_id}, client=client)
        assert response.status_code == 200, response.text
        packages = TypeAdapter(list[dict[str, JsonValue]]).validate_python(
            response.raise_for_status().json()["data"]["packages"]
        )
        return packages

    def has_package(item: dict[str, JsonValue]) -> bool:
        manifest = _JSON_OBJECT_ADAPTER.validate_python(item.get("manifest", {}))
        return manifest.get("package_id") == _PACKAGE_ID

    try:
        # 用本机到目标的路由地址，而不是 loopback，供 Windows 安装器经真实 HTTP 下载包。
        workspaces = client_a.get("/api/gateway/workspaces", headers={
            "X-Request-ID": "remote-plugin-projection",
        }).raise_for_status().json()["data"]["items"]
        projected, = [item for item in workspaces if item["gateway_id"] == gateway_b]
        connection_id = projected["remote_gateways"][0]["gateway_connection_id"]
        assert projected["workspace_id"]

        publish_policy({
            "service_plugin_installation_manage", "service_plugin_runtime_manage",
            "configuration_read", "configuration_write", "send", "read",
        })
        def configure_source(document: dict[str, JsonValue]) -> None:
            plugins = _JSON_OBJECT_ADAPTER.validate_python(document.get("plugins", {}))
            plugins["sources"] = {"entries": [{
                "source_id": "remote-ssh-proof",
                "protocol_version": 1,
                "base_url": f"http://{local_ip}:{catalog.port}",
                "enabled": True,
            }]}
            document["plugins"] = plugins

        edit_b(configure_source)
        source = PluginSourceCatalogDTO.model_validate(relay("POST", "/api/gateway/service-plugins/source-catalog", {
            "gateway_id": gateway_b, "source_id": "remote-ssh-proof",
        }).raise_for_status().json()["data"])
        artifacts = {item.package_version: item for item in source.items}
        assert set(artifacts) == {"1.0.0", "1.1.0"}
        assert not any(has_package(item) for item in installation_inventory(client_a, False))

        installed = install(artifacts["1.0.0"])
        assert installed.gateway_id == gateway_b
        bind("1.0.0")
        active, = [item for item in direct_status(client_b).plugins if item.binding.package_id == _PACKAGE_ID]
        assert active.state == "ready" and active.instance is not None
        instance_id, original_pid = active.instance.instance_id, active.instance.process_id
        direct_call = echo(client_b, remote=False, instance_id=instance_id, version="1.0.0")
        relayed_call = echo(client_a, remote=True, instance_id=instance_id, version="1.0.0")
        assert direct_call["process_id"] == relayed_call["process_id"] == original_pid
        assert not any(has_package(item) for item in installation_inventory(client_a, False))

        capabilities = {
            "service_plugin_installation_manage", "service_plugin_runtime_manage",
            "configuration_read", "configuration_write", "send", "read",
        }
        publish_policy(capabilities - {"send"})
        denied = relay("GET", f"/api/gateway/service-plugins/{_PACKAGE_ID}/{_PLUGIN_ID}/instances/"
                       f"{instance_id}/services/echo/?package_version=1.0.0", expected_status=403)
        assert denied.status_code == 403
        assert echo(client_b, remote=False, instance_id=instance_id, version="1.0.0")["instance_id"] == instance_id
        publish_policy(capabilities)

        install(artifacts["1.1.0"])
        unchanged, = [item for item in relayed_status().plugins if item.binding.package_id == _PACKAGE_ID]
        assert unchanged.binding.package_version == "1.0.0" and unchanged.instance is not None
        assert unchanged.instance.instance_id == instance_id
        assert echo(client_a, remote=True, instance_id=instance_id, version="1.0.0")["package_version"] == "1.0.0"

        bind("1.1.0")
        switched, = [item for item in relayed_status().plugins if item.binding.package_id == _PACKAGE_ID]
        assert switched.binding.package_version == "1.1.0" and switched.instance is not None
        replacement_id, replacement_pid = switched.instance.instance_id, switched.instance.process_id
        assert replacement_id != instance_id and replacement_pid != original_pid
        assert not _remote_process_alive(remote_alias, original_pid)
        old = relay("GET", f"/api/gateway/service-plugins/{_PACKAGE_ID}/{_PLUGIN_ID}/instances/"
                    f"{instance_id}/services/echo/?package_version=1.0.0", expected_status=409)
        assert old.status_code == 409
        assert echo(client_a, remote=True, instance_id=replacement_id, version="1.1.0")["package_version"] == "1.1.0"

        bind("1.1.0", enabled=False)
        stopped, = [item for item in relayed_status().plugins if item.binding.package_id == _PACKAGE_ID]
        assert stopped.state == "stopped" and stopped.instance is None
        assert not _remote_process_alive(remote_alias, replacement_pid)
        refused = relay("POST", "/api/gateway/service-plugins/uninstall", {
            "gateway_id": gateway_b, "package_id": _PACKAGE_ID, "package_version": "1.1.0",
        }, expected_status=409)
        assert refused.status_code == 409
        def unbind(document: dict[str, JsonValue]) -> None:
            plugins = _JSON_OBJECT_ADAPTER.validate_python(document.get("plugins", {}))
            plugins["runtime"] = {"entries": []}
            document["plugins"] = plugins

        edit_b(unbind)
        for version in ("1.0.0", "1.1.0"):
            relay("POST", "/api/gateway/service-plugins/uninstall", {
                "gateway_id": gateway_b, "package_id": _PACKAGE_ID, "package_version": version,
            })
        assert not any(has_package(item) for item in installation_inventory(client_b, False))
        assert not any(has_package(item) for item in installation_inventory(client_a, False))
        for version in ("1.0.0", "1.1.0"):
            package_path = remote_home.rstrip("/\\") + f"/plugins/packages/{_PACKAGE_ID}/{version}"
            assert not _remote_path_exists(remote_alias, package_path)
        (context.artifacts_dir / "remote-plugin-ssh-lifecycle.json").write_text(json.dumps({
            "gateway_ids": [gateway_a, gateway_b],
            "connection_id": connection_id,
            "original_instance_id": instance_id,
            "replacement_instance_id": replacement_id,
            "closed_process_ids": [original_pid, replacement_pid],
            "source_transport": "Windows B fetched the catalog and ZIP from Linux over the test network",
            "federation_transport": "OpenSSH remote pairing and local TCP forwarding to Windows B",
            "direct_and_relay_same_instance": True,
            "request_ids_preserved": True,
            "gateway_a_installation_empty": True,
            "revoked_request_status": denied.status_code,
            "evidence": evidence,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        catalog.close()
