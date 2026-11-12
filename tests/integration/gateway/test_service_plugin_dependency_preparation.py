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
import asyncio, json, os, sys
sys.path.insert(0, os.environ['BOXTEAM_PROJECT_ROOT'])
from fastapi import FastAPI, Request
import uvicorn
from app.core.logging_config import configure_application_logging
from app.runtime.plugin_commands import plugin_command_router
from plugin_dependency import VALUE
configure_application_logging(service='dependency-probe')
identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])
async def shutdown(): server.should_exit = True
app = FastAPI()
app.include_router(plugin_command_router({}, shutdown=shutdown))
@app.get('/services/echo/value')
async def echo(request: Request):
    return {'value': VALUE, 'instance_id': identity['instance_id'],
        'request_id': request.headers.get('X-Request-ID')}
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=int(os.environ['BOXTEAM_PLUGIN_PORT']), log_config=None))
asyncio.run(server.serve())
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
    lock_dependency_version: str | None = None, target: dict[str, object] | None = None) -> Path:
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
        "target": native_plugin_target("python") if target is None else target,
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
    for provider in payload["llm"]["providers"]:
        if "api_key" in provider:
            provider["api_key"] = "plugin-dependency-unused-key"
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

    def install(archive: Path):
        return install_plugin(
            lambda path, payload: exchange("POST", path, payload),
            gateway_id=gateway_id,
            path=str(archive),
            expected_digest=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )

    def expect_install_failure(archive: Path):
        return expect_plugin_install_failure(
            lambda path, payload: exchange("POST", path, payload),
            gateway_id=gateway_id,
            path=str(archive),
            expected_digest=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )

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
        assert install(packages["good"]).status == "succeeded"
        stale = expect_install_failure(packages["stale"])
        assert stale.error and "pyproject.toml 与 uv.lock 根依赖不一致" in stale.error
        missing = expect_install_failure(packages["missing"])
        assert missing.error and "缺少已准备的 python/site-packages" in missing.error

        inventory = exchange(
            "POST", "/api/gateway/service-plugins/inventory", {"gateway_id": gateway_id},
        ).raise_for_status().json()["data"]["packages"]
        assert {item["manifest"]["version"] for item in inventory} == {"1.0.0"}

        ready = bind()
        assert ready["state"] == "ready"
        reply = call(ready).raise_for_status().json()
        assert reply["value"] == "locked-dependency-ready"
        assert reply["instance_id"] == ready["instance"]["instance_id"]

        native_target = native_plugin_target("python")
        for field, value in (("os", "windows" if native_target["os"] != "windows" else "linux"),
                             ("architecture", "arm64" if native_target["architecture"] == "x64" else "x64"),
                             ("version", "0.0.0"), ("abi", "cp000")):
            target = json.loads(json.dumps(native_target))
            (target["python"] if field in {"version", "abi"} else target)[field] = value
            incompatible = _plugin_archive(context.artifacts_dir / f"incompatible-{field}.zip",
                package_version=f"4.0.{len(evidence)}", target=target)
            rejected = expect_install_failure(incompatible)
            assert rejected.error and "package=" in rejected.error and "gateway=" in rejected.error
            assert "完整插件 ZIP" in rejected.error
            assert next(item for item in status()["plugins"] if item["binding"]["plugin_id"] == "probe") == ready
            assert call(ready).raise_for_status().json()["value"] == "locked-dependency-ready"
        inventory = exchange("POST", "/api/gateway/service-plugins/inventory",
            {"gateway_id": gateway_id}).raise_for_status().json()["data"]["packages"]
        assert {item["manifest"]["version"] for item in inventory} == {"1.0.0"}
        source_path = "/api/gateway/config/sources/gateway_mutable_override"
        source = exchange("GET", source_path).raise_for_status().json()["data"]
        candidate = commentjson.loads(source["jsonc"])
        candidate["plugins"]["runtime"]["entries"][0]["package_version"] = "99.0.0"
        missing_version = exchange("PUT", source_path, {
            "gateway_id": gateway_id, "source_key": "gateway_mutable_override",
            "jsonc": json.dumps(candidate), "base_file_digest": source["file_digest"],
            "base_layer_revision": source["layer_revision"], "base_layer_digest": source["layer_digest"],
            "expected_active_revision": source["active_revision"], "expected_active_digest": source["active_digest"],
            "idempotency_key": "plugin-dependency-missing-version",
        })
        assert missing_version.status_code == 422
        assert "dependency.probe@99.0.0" in missing_version.text and "此确切版本" in missing_version.text
        retained_source = exchange("GET", source_path).raise_for_status().json()["data"]
        for field in ("file_digest", "layer_revision", "layer_digest", "active_revision", "active_digest"):
            assert retained_source[field] == source[field]
        assert next(item for item in status()["plugins"] if item["binding"]["plugin_id"] == "probe") == ready
        assert call(ready).raise_for_status().json()["value"] == "locked-dependency-ready"

        restarted = exchange("POST", "/api/gateway/service-plugins/restart", {
            "gateway_id": gateway_id,
            "package_id": "dependency.probe",
            "package_version": "1.0.0",
            "plugin_id": "probe",
            "revision": ready["revision"],
        }).raise_for_status().json()["data"]
        assert restarted["instance"]["instance_id"] != ready["instance"]["instance_id"]
        assert call(restarted).raise_for_status().json()["value"] == "locked-dependency-ready"
        changed = expect_install_failure(packages["changed"])
        assert changed.error and "摘要冲突" in changed.error, "同包版本内容不可变"
        assert call(restarted).raise_for_status().json()["value"] == "locked-dependency-ready"
    finally:
        (context.artifacts_dir / "plugin-dependency-flow.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
