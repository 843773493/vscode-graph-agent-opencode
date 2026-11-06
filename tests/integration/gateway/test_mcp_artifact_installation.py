"""真实 Gateway／双 Backend／uv wheel／配置 CAS／Thread 工具调用的纵向切片。"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import commentjson
import httpx
import pytest

from app.schemas.internal_v2.mcp import McpArtifactReferencesResponseDTO
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式构建由同一会话复用
)
from tests.support.http_server import running_http_server
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


def _wheel(index: Path, name: str, version: str, code: bytes, *, requires: str = "", entrypoint: str = "") -> None:
    """生成标准 wheel 测试输入；真正解析、校验和安装仍由生产 uv 执行。"""
    module = name.replace("-", "_")
    metadata = f"{module}-{version}.dist-info"
    files = {f"{module}.py": code, f"{metadata}/WHEEL": b"Wheel-Version: 1.0\nGenerator: boxteam-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{metadata}/METADATA": f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n{requires}\n".encode()}
    if entrypoint:
        files[f"{metadata}/entry_points.txt"] = f"[console_scripts]\n{entrypoint} = {module}:main\n".encode()
    records = io.StringIO()
    writer = csv.writer(records)
    for filename, data in files.items():
        writer.writerow((filename, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("="), len(data)))
    writer.writerow((f"{metadata}/RECORD", "", ""))
    files[f"{metadata}/RECORD"] = records.getvalue().encode()
    directory = index / "simple" / name
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{module}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(directory / filename, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    wheels = sorted(directory.glob("*.whl"))
    (directory / "index.html").write_text("\n".join(f'<a href="{wheel.name}#sha256={hashlib.sha256(wheel.read_bytes()).hexdigest()}">{wheel.name}</a>' for wheel in wheels), encoding="utf-8")


@dataclass(frozen=True)
class ArtifactIndex:
    root: Path
    url: str
    requests: list[str]

    def source(self, version: str = "release-1") -> dict[str, str]:
        return {"kind": "gallery", "name": "org.example/artifact", "version": version, "endpoint": self.url,
            "entry_url": self.url + "/v0.1/servers/org.example%2Fartifact/versions/" + version}


@pytest.fixture
def artifact_download_gate() -> tuple[threading.Event, threading.Event] | None:
    return None


@pytest.fixture
def artifact_index(request: pytest.FixtureRequest,
    artifact_download_gate: tuple[threading.Event, threading.Event] | None) -> Iterator[ArtifactIndex]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = context.artifacts_dir / "index"
    _wheel(root, "locked-leaf", "1.0.0", b"VALUE = 'locked-one'\n")
    _wheel(root, "artifact-server", "1.0.0", (Path.cwd() / "tests/integration/workspace_services/mcp/artifact_server.py").read_bytes(),
        requires="Requires-Dist: locked-leaf>=1\n", entrypoint="artifact-server")
    requests: list[str] = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, format: str, *arguments: object) -> None:
            requests.append(format % arguments)

        def end_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def do_GET(self) -> None:
            if artifact_download_gate is not None:
                entered, release = artifact_download_gate
                if self.path == "/download-state":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"started": entered.is_set()}).encode())
                    return
                if urlsplit(self.path).path.endswith(".whl"):
                    entered.set()
                    assert release.wait(90), "测试未释放真实 wheel 下载"
            if urlsplit(self.path).path != "/v0.1/servers":
                super().do_GET()
                return
            document = json.loads(manifest.read_text(encoding="utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"servers": [document], "metadata": {"count": 1}}).encode())

    try:
        with running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root)))) as server:
            index = ArtifactIndex(root, f"http://127.0.0.1:{server.server_port}", requests)
            manifest = root / "v0.1/servers/org.example/artifact/versions/release-1"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({"server": {"name": "org.example/artifact", "title": "Artifact Server", "description": "标准 wheel 安装验证", "version": "release-1", "packages": [
                {"registryType": "pypi", "identifier": "artifact-server", "version": "1.0.0", "registryBaseUrl": index.url + "/simple",
                 "transport": {"type": "stdio"}}]}, "_meta": {}}), encoding="utf-8")
            try:
                yield index
            finally:
                if artifact_download_gate is not None:
                    artifact_download_gate[1].set()
    finally:
        (context.artifacts_dir / "index-requests.json").write_text(json.dumps(requests, indent=2), encoding="utf-8")


@dataclass(frozen=True)
class InstallationCase:
    client: httpx.Client
    context: TestRunContext
    workspace: Path
    home: Path
    replay: ModelStreamReplay


@pytest.fixture
def installation_case(request: pytest.FixtureRequest, web_workbench_assets: Path) -> Iterator[InstallationCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    # 工作区业务状态与 node home 同步隔离，不能把另一 Backend 的受管配置带入新场景。
    workspace = prepare_test_workspace(workspace_root=context.workspace_root / request.node.name,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=())
    install_test_workspace_config(workspace_root=workspace, config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
        schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    config_path = workspace / ".boxteam/workspace.jsonc"
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    provider = next(item for item in config["llm"]["providers"] if item["id"] == "primary")
    provider.update({"model": "e2e-stub-model", "api_key": "artifact-fixture-key"})
    config["llm"]["providers"] = [provider]
    config["agents"]["default"]["model"] = {"primary_provider": "primary", "fallback_providers": []}
    # MCP 回放场景不使用网页 embedding；关闭其对已移除 Provider 的独立依赖。
    fetch = next(tool for tool in config["agents"]["default"]["tools"]["custom"] if tool["tool_id"] == "fetch_webpage")
    fetch["options"]["embedding"] = None
    config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
    runtime = context.runtime_root_for_node(request.node.nodeid)
    home = context.boxteam_home_for_node(request.node.nodeid)
    port = integration_port_block_for_file(Path(request.node.path)).port(0)
    with model_stream_replay(context.artifacts_dir, runtime_root=runtime, business_assertion=request.node.nodeid) as replay:
        gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None, port=port,
            boxteam_home=home, artifacts_dir=context.artifacts_dir, asgi_app="tests.support.e2e_control.gateway_app:app",
            extra_env={**replay.environment, "BOXTEAM_WEB_ASSETS": str(web_workbench_assets)})
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=LOCAL_TOKEN_HEADERS, timeout=30, trust_env=False) as client:
                client.post("/api/gateway/users/guest", json={"tracking": {"test": True}}).raise_for_status()
                yield InstallationCase(client, context, workspace, home, replay)
        finally:
            close_gateway_process(gateway)


def test_real_installation_publishes_locked_generation_and_original_thread_uses_it(installation_case: InstallationCase, artifact_index: ArtifactIndex) -> None:
    case = installation_case
    evidence: list[dict[str, object]] = []

    def exchange(method: str, path: str, key: str, body: dict[str, object] | None = None) -> dict:
        response = case.client.request(method, path, json=body, headers={"X-Request-ID": key})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"] == key
        evidence.append({"path": path, "method": method, "request_id": key, "response": payload})
        return payload

    workspaces = exchange("GET", "/api/gateway/workspaces", "artifact-workspaces")["data"]
    workspace_id = workspaces["items"][0]["workspace_id"]
    base = f"/api/v1/workspaces/{workspace_id}"
    original = (case.workspace / ".boxteam/workspace.jsonc").read_bytes()
    capabilities = exchange("GET", base + "/mcp/installation-capabilities", "artifact-capabilities")
    target = capabilities["target"]
    assert capabilities["data"]["pypi_wheel_available"] is True
    inspected = exchange("POST", base + "/mcp/candidate-manifests", "artifact-inspect", {"target": target, "source": artifact_index.source()})["data"]
    candidate = {"target": target, "source": artifact_index.source(), "manifest_digest": inspected["manifest_digest"], "variant": inspected["variants"][0]["ref"]}
    plan = exchange("POST", base + "/mcp/candidate-plans", "artifact-plan", candidate)["data"]
    intent = {"candidate": candidate, "plan_digest": plan["plan_digest"], "operation_id": "real-artifact", "entrypoint": "artifact-server"}
    operation = exchange("POST", base + "/mcp/installations", "artifact-install", intent)["data"]
    deadline = time.monotonic() + 40
    while operation["status"] == "running":
        assert time.monotonic() < deadline, operation
        time.sleep(0.1)
        operation = exchange("GET", base + "/mcp/installation-operations/real-artifact", "artifact-observe")["data"]
    assert operation["status"] == "succeeded", operation
    artifact = operation["artifact"]
    command = Path(artifact["command"])
    assert command.is_relative_to(case.home / "artifacts/mcp" / target["backend_id"])
    assert artifact["binding"]["generation"] in command.parts and command.is_file()
    assert artifact["installed_distributions"] == {"artifact-server": "1.0.0", "locked-leaf": "1.0.0"}
    environment = command.parent.parent
    lock = environment.parent / "uv.lock"
    assert artifact["lock_digest"] == "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
    assert (case.workspace / ".boxteam/workspace.jsonc").read_bytes() == original
    assert exchange("GET", base + "/mcp/servers", "artifact-not-configured")["data"] == []
    assert exchange("POST", base + "/mcp/installations", "artifact-repeat", intent)["data"] == operation
    source_path = base + "/config/sources/workspace_root_mutable_override"
    document = exchange("GET", source_path, "artifact-source")["data"]
    configuration = commentjson.loads(document["jsonc"])
    configuration["mcp"] = {"servers": {"installed": {"enabled": True, "transport": "stdio", "managed_launch": artifact["binding"]}}}
    edit = {"gateway_id": document["gateway_id"], "workspace_id": document["workspace_id"], "source_key": document["source_key"], "jsonc": json.dumps(configuration),
        "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
        "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"], "idempotency_key": "bind-artifact"}
    exchange("POST", source_path + "/preflight", "artifact-preflight", edit)
    exchange("PUT", source_path, "artifact-configure", edit)
    servers = exchange("GET", base + "/mcp/servers", "artifact-connected")["data"]
    assert len(servers) == 1 and servers[0]["status"] == "ready"
    _wheel(artifact_index.root, "locked-leaf", "2.0.0", b"VALUE = 'changed-index-two'\n")
    session = exchange("POST", base + "/sessions", "artifact-session", {"title": "锁定 MCP 制品真实调用"})["data"]
    replies = [{"tool_calls": [{"index": 0, "id": "artifact-probe", "type": "function", "function": {
        "name": "invoke_extension_tool", "arguments": json.dumps({"tool_name": "mcp__installed__installation_probe", "arguments": {}})}}]}, {"content": "已核实安装制品。"}]
    calls = case.replay.control.plan_model_calls(session["session_id"], session["thread_id"], [cast(list[dict[str, object]], build_cassette(
        asset_id=f"artifact-{index}", model="e2e-stub-model", url="https://opencode.ai/zen/v1/chat/completions",
        frames=chat_completion_frames(response_id=f"artifact-{index}", model="e2e-stub-model", deltas=({"role": "assistant", **reply},),
            finish_reason="tool_calls" if "tool_calls" in reply else "stop")).raw["interactions"])[0] for index, reply in enumerate(replies)])
    accepted = exchange("POST", base + f"/sessions/{session['session_id']}/messages", "artifact-message", {
        "message": {"content": "调用安装探针，核对目标路径和锁定依赖。"}, "run": {"mode": "single_agent", "agent_id": "default"}})["data"]
    deadline = time.monotonic() + 40
    while True:
        job = exchange("GET", base + f"/jobs/{accepted['job_id']}", "artifact-job")["data"]
        if job["status"] in {"succeeded", "completed"}:
            break
        assert job["status"] not in {"failed", "cancelled", "timed_out"} and time.monotonic() < deadline, job
        time.sleep(0.1)
    wire = json.loads((case.replay.artifacts / f"{calls[1]}.json").read_text(encoding="utf-8"))["request"]
    tool_message, = [message for message in wire["messages"] if message["role"] == "tool"]
    assert tool_message["tool_call_id"] == "artifact-probe"
    assert tool_message["name"] == "invoke_extension_tool"
    part, = tool_message["content"]
    assert part["type"] == "text"
    actual = json.loads(part["text"])
    assert actual == {"prefix": str(environment), "executable": str(environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")), "dependency": "locked-one"}
    assert artifact["binding"]["generation"] in Path(actual["executable"]).parts
    secondary = prepare_test_workspace(workspace_root=case.context.workspace_root / "shared-backend-B",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=())
    install_test_workspace_config(workspace_root=secondary, config_path=case.workspace / ".boxteam/workspace.jsonc",
        schema_path=case.workspace / ".boxteam/workspace_schema.jsonc")
    # B 是同 home 的另一个真实 Backend，不改既有目标拓扑；配置在注册前保持独立且无服务。
    secondary_config = commentjson.loads((secondary / ".boxteam/workspace.jsonc").read_text(encoding="utf-8"))
    secondary_config["mcp"] = {"servers": {}}
    (secondary / ".boxteam/workspace.jsonc").write_text(json.dumps(secondary_config), encoding="utf-8")
    registered = exchange("POST", "/api/gateway/managed-workspaces", "artifact-second-backend", {"root_path": str(secondary), "name": "共享制品 B"})["data"]
    second_id = next(item["workspace_id"] for item in registered["items"] if item["workspace_id"] != workspace_id)
    second_base = f"/api/v1/workspaces/{second_id}"
    assert exchange("GET", second_base + "/mcp/installation-capabilities", "artifact-second-capabilities")["target"] == target
    assert exchange("GET", second_base + "/mcp/installations", "artifact-second-shared")["data"] == [artifact]
    assert exchange("GET", second_base + "/mcp/servers", "artifact-second-independent")["data"] == []
    original_servers = exchange("GET", base + "/mcp/servers", "artifact-original-still-ready")["data"]
    assert len(original_servers) == 1 and original_servers[0]["status"] == "ready"
    (case.context.artifacts_dir / "installation-evidence.json").write_text(json.dumps({"http": evidence, "artifact": artifact,
        "tool_wire": tool_message, "second_workspace": second_id}, ensure_ascii=False, indent=2), encoding="utf-8")


def test_original_api_retains_shared_generation_until_both_workspaces_release_it(
    installation_case: InstallationCase, artifact_index: ArtifactIndex,
) -> None:
    case = installation_case
    evidence: list[dict[str, object]] = []

    def exchange(method: str, path: str, key: str, body: dict[str, object] | None = None, *, status: int = 200) -> dict:
        response = case.client.request(method, path, json=body, headers={"X-Request-ID": key})
        assert response.status_code == status, response.text
        result = response.json()
        assert response.headers["X-Request-ID"] == result["request_id"] == key
        evidence.append({"method": method, "path": path, "status": status, "response": result})
        if status == 409:
            McpArtifactReferencesResponseDTO.model_validate(result)
            assert response.headers["Cache-Control"] == "no-store"
            assert "artifact-fixture-key" not in response.text and str(case.workspace) not in response.text
        return result

    workspace_id = exchange("GET", "/api/gateway/workspaces", "retention-workspaces")["data"]["items"][0]["workspace_id"]
    base = f"/api/v1/workspaces/{workspace_id}"
    target = exchange("GET", base + "/mcp/installation-capabilities", "retention-capabilities")["target"]
    inspected = exchange("POST", base + "/mcp/candidate-manifests", "retention-inspect", {"target": target, "source": artifact_index.source()})["data"]
    candidate = {"target": target, "source": artifact_index.source(), "manifest_digest": inspected["manifest_digest"], "variant": inspected["variants"][0]["ref"]}
    plan = exchange("POST", base + "/mcp/candidate-plans", "retention-plan", candidate)["data"]
    operation = exchange("POST", base + "/mcp/installations", "retention-install", {
        "candidate": candidate, "plan_digest": plan["plan_digest"], "operation_id": "shared-install", "entrypoint": "artifact-server"})["data"]
    deadline = time.monotonic() + 40
    while operation["status"] == "running":
        assert time.monotonic() < deadline, operation
        time.sleep(.1)
        operation = exchange("GET", base + "/mcp/installation-operations/shared-install", "retention-install-observe")["data"]
    assert operation["status"] == "succeeded", operation
    artifact = operation["artifact"]
    command = Path(artifact["command"])
    assert command.is_file()

    second = prepare_test_workspace(workspace_root=case.context.workspace_root / "retention-A2",
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=())
    install_test_workspace_config(workspace_root=second, config_path=case.workspace / ".boxteam/workspace.jsonc",
        schema_path=case.workspace / ".boxteam/workspace_schema.jsonc")
    registered = exchange("POST", "/api/gateway/managed-workspaces", "retention-register-A2", {"root_path": str(second), "name": "共享制品 A2"})["data"]
    second_id = next(item["workspace_id"] for item in registered["items"] if item["workspace_id"] != workspace_id)
    second_base = f"/api/v1/workspaces/{second_id}"
    assert exchange("GET", second_base + "/mcp/installation-capabilities", "retention-A2-target")["target"] == target

    def configure(prefix: str, server: dict[str, object] | None, key: str) -> None:
        path = prefix + "/config/sources/workspace_root_mutable_override"
        document = exchange("GET", path, key + "-source")["data"]
        configuration = commentjson.loads(document["jsonc"])
        configuration["mcp"] = {"servers": {"shared": server} if server is not None else {}}
        edit = {"gateway_id": document["gateway_id"], "workspace_id": document["workspace_id"], "source_key": document["source_key"], "jsonc": json.dumps(configuration),
            "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"], "idempotency_key": key}
        exchange("POST", path + "/preflight", key + "-preflight", edit)
        exchange("PUT", path, key + "-save", edit)

    server = {"enabled": True, "transport": "stdio", "managed_launch": artifact["binding"]}
    configure(base, server, "retention-bind-A1")
    configure(second_base, server, "retention-bind-A2")
    first_server, = exchange("GET", base + "/mcp/servers", "retention-A1-ready")["data"]
    second_server, = exchange("GET", second_base + "/mcp/servers", "retention-A2-ready")["data"]
    first_ref, second_ref = first_server["connection_ref"], second_server["connection_ref"]
    assert first_server["status"] == second_server["status"] == "ready" and first_ref != second_ref
    second_config = (second / ".boxteam/workspace.jsonc").read_bytes()
    second_resources = (second / ".boxteam/resources.json").read_bytes()
    uninstall_path = base + "/mcp/installations/" + artifact["binding"]["installation_id"]
    intent = {"target": target, "binding": artifact["binding"], "operation_id": "shared-uninstall"}
    retained = exchange("GET", uninstall_path + "/references", "retention-both-references")["data"]["references"]
    assert {reference["workspace_id"] for reference in retained} == {workspace_id, second_id}
    assert {reference["connection_ref"] for reference in retained if reference["kind"] == "connection"} == {first_ref, second_ref}

    configure(base, None, "retention-remove-A1")
    refused = exchange("DELETE", uninstall_path, "retention-A2-refusal", intent, status=409)["data"]
    assert refused["binding"] == artifact["binding"]
    assert any(reference["workspace_id"] == second_id and reference["connection_ref"] == second_ref for reference in refused["references"])
    assert command.is_file() and (second / ".boxteam/workspace.jsonc").read_bytes() == second_config
    assert (second / ".boxteam/resources.json").read_bytes() == second_resources
    assert exchange("GET", second_base + "/mcp/servers", "retention-A2-unchanged")["data"] == [second_server]
    configure(second_base, None, "retention-remove-A2")
    deadline = time.monotonic() + 15
    while True:
        references = exchange("GET", uninstall_path + "/references", "retention-draining")["data"]["references"]
        if not references:
            break
        assert time.monotonic() < deadline, references
        time.sleep(.05)
    for root, connection_ref in ((case.workspace, first_ref), (second, second_ref)):
        ledger = ExternalResourceLeaseLedger(state_path=root / ".boxteam/resources.json")
        record = ledger.get(connection_ref)
        assert record is not None and record.managed_launch is not None and record.managed_launch.model_dump() == artifact["binding"]
        assert record.status == "stopped" and record.process is not None and record.process.exit_verified
        assert not record.cleanup_failed and ledger.leases_for_resource(connection_ref) == []
    removing = exchange("DELETE", uninstall_path, "retention-uninstall", intent)["data"]
    assert removing["kind"] == "uninstall" and removing["artifact"]["state"] == "removing"
    deadline = time.monotonic() + 10
    while removing["status"] == "running":
        assert time.monotonic() < deadline, removing
        time.sleep(.05)
        removing = exchange("GET", base + "/mcp/installation-operations/shared-uninstall", "retention-uninstall-observe")["data"]
    assert removing["status"] == "succeeded" and removing["artifact"]["state"] == "removed", removing
    assert not command.parent.parent.parent.exists()
    assert exchange("DELETE", uninstall_path, "retention-repeat", intent)["data"] == removing
    assert exchange("GET", second_base + "/mcp/installations", "retention-removed-in-A2")["data"] == []
    (case.context.artifacts_dir / "retention-evidence.json").write_text(json.dumps({"http": evidence,
        "connections": [first_ref, second_ref], "artifact": artifact}, ensure_ascii=False, indent=2), encoding="utf-8")


def test_same_source_update_is_separate_until_original_candidate_cas_explicitly_switches_idle_thread(
    installation_case: InstallationCase, artifact_index: ArtifactIndex,
) -> None:
    case = installation_case
    evidence: list[dict[str, object]] = []

    def exchange(method: str, path: str, key: str, body: dict[str, object] | None = None) -> dict:
        response = case.client.request(method, path, json=body, headers={"X-Request-ID": key})
        assert response.status_code == 200, response.text
        result = response.json()
        assert response.headers["X-Request-ID"] == result["request_id"] == key
        evidence.append({"method": method, "path": path, "request_id": key, "response": result})
        return result["data"]

    workspace_id = exchange("GET", "/api/gateway/workspaces", "update-workspaces")["items"][0]["workspace_id"]
    base = f"/api/v1/workspaces/{workspace_id}"
    capabilities = case.client.get(base + "/mcp/installation-capabilities")
    capabilities.raise_for_status()
    target = capabilities.json()["target"]
    source_path = base + "/config/sources/workspace_root_mutable_override"

    def candidate_plan(version: str) -> dict:
        source = artifact_index.source(version)
        inspected = exchange("POST", base + "/mcp/candidate-manifests", "update-inspect-" + version, {"target": target, "source": source})
        candidate = {"target": target, "source": source, "manifest_digest": inspected["manifest_digest"],
            "variant": inspected["variants"][0]["ref"], "server_id": "updatable"}
        plan = exchange("POST", base + "/mcp/candidate-plans", "update-plan-" + version, candidate)
        return {"candidate": candidate, "plan_digest": plan["plan_digest"]}

    def install(plan: dict, key: str) -> dict:
        operation = exchange("POST", base + "/mcp/installations", key, {**plan, "operation_id": key, "entrypoint": "artifact-server"})
        deadline = time.monotonic() + 40
        while operation["status"] == "running":
            assert time.monotonic() < deadline, operation
            time.sleep(.1)
            operation = exchange("GET", base + "/mcp/installation-operations/" + key, key + "-observe")
        return operation

    def configuration_request(plan: dict, artifact: dict, key: str) -> dict:
        document = exchange("GET", source_path, key + "-source")
        configuration = commentjson.loads(document["jsonc"])
        configuration["mcp"] = {"servers": {"updatable": {"enabled": True, "transport": "stdio", "managed_launch": artifact["binding"]}}}
        edit = {"gateway_id": document["gateway_id"], "workspace_id": document["workspace_id"], "source_key": document["source_key"], "jsonc": json.dumps(configuration),
            "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"], "idempotency_key": key}
        return {
            "target": {"kind": "workspace", "gateway_id": target["gateway_id"], "workspace_id": workspace_id},
            "server_id": "updatable", "plan": plan, "managed_launch": artifact["binding"], "source_edit": edit}

    def configure(plan: dict, artifact: dict, key: str) -> dict:
        request = configuration_request(plan, artifact, key)
        saved = exchange("PUT", base + "/mcp/candidate-configurations", key, request)
        assert saved["edit"]["state"] == "active" and saved["edit"]["source_saved"] and saved["edit"]["layer_imported"]
        assert saved["active_revision"] > request["source_edit"]["expected_active_revision"]
        assert saved["effective"]["mcp"]["servers"]["updatable"]["managed_launch"] == artifact["binding"]
        return saved

    first_plan = candidate_plan("release-1")
    first_operation = install(first_plan, "update-install-one")
    assert first_operation["status"] == "succeeded", first_operation
    first = first_operation["artifact"]
    first_document = configure(first_plan, first, "update-configure-one")
    first_server, = exchange("GET", base + "/mcp/servers", "update-ready-one")
    assert first_server["server_id"] == "updatable" and first_server["status"] == "ready"
    first_ref = first_server["connection_ref"]
    original_config = (case.workspace / ".boxteam/workspace.jsonc").read_bytes()
    session = exchange("POST", base + "/sessions", "update-session", {"title": "同来源制品显式升级"})
    probes: dict[str, dict] = {}

    def invoke(phase: str, artifact: dict, dependency: str, while_sealed: Callable[[], None] | None = None) -> dict:
        call_id = "update-probe-" + phase
        replies = [{"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {
            "name": "invoke_extension_tool", "arguments": json.dumps({"tool_name": "mcp__updatable__installation_probe", "arguments": {}})}}]},
            {"content": "已读取实际安装环境。"}]
        calls = case.replay.control.plan_model_calls(session["session_id"], session["thread_id"], [cast(list[dict[str, object]], build_cassette(
            asset_id=f"update-{phase}-{index}", model="e2e-stub-model", url="https://opencode.ai/zen/v1/chat/completions",
            frames=chat_completion_frames(response_id=f"update-{phase}-{index}", model="e2e-stub-model", deltas=({"role": "assistant", **reply},),
                finish_reason="tool_calls" if "tool_calls" in reply else "stop")).raw["interactions"])[0] for index, reply in enumerate(replies)])
        if while_sealed is not None:
            case.replay.control.arm(calls[0], "model.response_ready")
        accepted = exchange("POST", base + f"/sessions/{session['session_id']}/messages", "update-message-" + phase, {
            "message": {"content": "调用安装探针，读取当前精确制品的路径与依赖。"}, "run": {"mode": "single_agent", "agent_id": "default"}})
        if while_sealed is not None:
            try:
                case.replay.control.acknowledged(calls[0], "model.response_ready", 20)
                while_sealed()
            finally:
                case.replay.control.release(calls[0], "model.response_ready")
        deadline = time.monotonic() + 40
        while True:
            job = exchange("GET", base + f"/jobs/{accepted['job_id']}", "update-job-" + phase)
            if job["status"] in {"succeeded", "completed"}:
                break
            assert job["status"] not in {"failed", "cancelled", "timed_out"} and time.monotonic() < deadline, job
            time.sleep(.1)
        wire = json.loads((case.replay.artifacts / f"{calls[1]}.json").read_text(encoding="utf-8"))["request"]
        results = {message["tool_call_id"]: message for message in wire["messages"] if message["role"] == "tool"}
        tool_message = results[call_id]
        assert tool_message["name"] == "invoke_extension_tool"
        part, = tool_message["content"]
        environment = Path(artifact["command"]).parent.parent
        assert part["type"] == "text" and json.loads(part["text"]) == {"prefix": str(environment),
            "executable": str(environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")), "dependency": dependency}
        probes[phase] = tool_message
        evidence.append({"phase": phase, "session_id": session["session_id"], "thread_id": session["thread_id"],
            "model_call_ids": calls, "tool_results": results})
        return results

    invoke("original", first, "locked-one")
    ledger_path = case.workspace / ".boxteam/resources.json"
    first_record = ExternalResourceLeaseLedger(state_path=ledger_path).get(first_ref)
    assert first_record is not None and first_record.process is not None and not first_record.process.exit_verified

    def require_original(key: str) -> None:
        assert (case.workspace / ".boxteam/workspace.jsonc").read_bytes() == original_config
        current = exchange("GET", source_path, key + "-source")
        for field in ("file_digest", "layer_revision", "layer_digest", "source_generation", "active_revision", "active_digest", "effective"):
            assert current[field] == first_document[field], field
        assert exchange("GET", base + "/mcp/servers", key + "-connection") == [first_server]
        record = ExternalResourceLeaseLedger(state_path=ledger_path).get(first_ref)
        assert record is not None and record.status == "running" and not record.cleanup_failed
        assert record.process == first_record.process and record.managed_launch is not None
        assert record.managed_launch.model_dump() == first["binding"] and Path(first["command"]).is_file()

    manifest_path = artifact_index.root / "v0.1/servers/org.example/artifact/versions/release-2"
    manifest = json.loads(manifest_path.with_name("release-1").read_text(encoding="utf-8"))
    manifest["server"]["version"] = "release-2"
    manifest["server"]["packages"][0]["version"] = "2.0.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    second_plan = candidate_plan("release-2")
    failed = install(second_plan, "update-install-missing-two")
    assert failed["status"] == "failed" and failed["stage"] == "lock" and failed["artifact"] is None, failed
    assert failed["error"] and "artifact-server==2.0.0" in failed["log"]
    assert exchange("GET", base + "/mcp/installations", "update-failed-unpublished") == [first]
    require_original("update-after-failure")
    invoke("after-failure", first, "locked-one")

    _wheel(artifact_index.root, "locked-leaf", "2.0.0", b"VALUE = 'changed-index-two'\n")
    _wheel(artifact_index.root, "artifact-server", "2.0.0", (Path.cwd() / "tests/integration/workspace_services/mcp/artifact_server.py").read_bytes(),
        requires="Requires-Dist: locked-leaf>=2\n", entrypoint="artifact-server")
    second_operation = install(second_plan, "update-install-two")
    assert second_operation["status"] == "succeeded", second_operation
    second = second_operation["artifact"]
    assert second["installed_distributions"] == {"artifact-server": "2.0.0", "locked-leaf": "2.0.0"}
    assert second["source"]["name"] == first["source"]["name"] and second["source"]["endpoint"] == first["source"]["endpoint"]
    assert second["source"]["version"] == "release-2" and first["source"]["version"] == "release-1"
    assert second["binding"]["installation_id"] != first["binding"]["installation_id"]
    assert second["binding"]["generation"] != first["binding"]["generation"]
    assert Path(second["command"]).is_file()
    require_original("update-published-unconfigured")
    invoke("published-unconfigured", first, "locked-one")

    def reject_busy_switch() -> None:
        leases = ExternalResourceLeaseLedger(state_path=ledger_path).leases_for_resource(first_ref)
        assert any(lease.lease_id.startswith("mcp-prepared:v1:") for lease in leases)
        assert all(lease.status == "active" for lease in leases)
        request = configuration_request(second_plan, second, "update-busy-switch")
        for endpoint, body, key in (
            (base + "/mcp/candidate-configurations", request, "update-busy-candidate"),
            (source_path + "/preflight", request["source_edit"], "update-busy-preflight"),
            (source_path, request["source_edit"], "update-busy-source"),
        ):
            response = case.client.request("POST" if endpoint.endswith("/preflight") else "PUT", endpoint,
                json=body, headers={"X-Request-ID": key})
            assert response.status_code == 409, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"] == key
            assert "未结清" in response.text
            evidence.append({"method": response.request.method, "path": endpoint, "response": response.json(), "status": 409})
            require_original(key)

    invoke("busy-upgrade-rejected", first, "locked-one", reject_busy_switch)
    require_original("update-settled-without-auto-switch")
    ledger = ExternalResourceLeaseLedger(state_path=ledger_path)
    assert ledger.leases_for_resource(first_ref) == []
    second_document = configure(second_plan, second, "update-configure-two")
    assert second_document["active_revision"] > first_document["active_revision"]
    second_server, = exchange("GET", base + "/mcp/servers", "update-ready-two")
    assert second_server["status"] == "ready" and second_server["connection_ref"] != first_ref
    results = invoke("configured-two", second, "changed-index-two")
    assert all(results["update-probe-" + phase] == result for phase, result in probes.items())
    ledger = ExternalResourceLeaseLedger(state_path=ledger_path)
    previous = ledger.get(first_ref)
    assert previous is not None and previous.status == "stopped" and previous.process is not None and previous.process.exit_verified
    assert previous.managed_launch is not None and previous.managed_launch.model_dump() == first["binding"]
    assert not previous.cleanup_failed and ledger.leases_for_resource(first_ref) == []
    current = ledger.get(second_server["connection_ref"])
    assert current is not None and current.status == "running" and current.managed_launch is not None
    assert current.managed_launch.model_dump() == second["binding"]
    assert ledger.leases_for_resource(second_server["connection_ref"]) == []
    artifacts = exchange("GET", base + "/mcp/installations", "update-both-published")
    assert {artifact["binding"]["generation"] for artifact in artifacts} == {first["binding"]["generation"], second["binding"]["generation"]}
    assert Path(first["command"]).is_file() and all(artifact["state"] == "published" for artifact in artifacts)
    (case.context.artifacts_dir / "update-evidence.json").write_text(json.dumps({"http": evidence,
        "first": first, "second": second, "old_connection": first_ref, "new_connection": second_server["connection_ref"]},
        ensure_ascii=False, indent=2), encoding="utf-8")


def test_original_workbench_install_action_publishes_without_configuring(installation_case: InstallationCase, artifact_index: ArtifactIndex) -> None:
    case = installation_case
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "MCP 制品安装产品验收需要真实 Chromium"
    original = (case.workspace / ".boxteam/workspace.jsonc").read_bytes()
    command = ["bun", "tests/integration/clients/web/mcp_artifact_installation.mjs"]
    protected = ["timeout", "150", "bash", "-c", 'ulimit -d 4194304; exec "$@"', "bash", *command]
    with chromium_temporary_environment(case.context.artifacts_dir / "chromium-tmp") as temporary:
        result = subprocess.run(protected, timeout=165, check=False, capture_output=True, text=True, env={**os.environ, **temporary,
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable, "BOXTEAM_ARTIFACT_FIXTURE": json.dumps({
                "baseUrl": str(case.client.base_url).rstrip("/"), "registryUrl": artifact_index.url,
                "artifacts": str(case.context.artifacts_dir), "testFile": "tests/integration/gateway/test_mcp_artifact_installation.py"})})
    (case.context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (case.context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    (case.context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "protected_command": protected,
        "exit_code": result.returncode}, indent=2), encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((case.context.artifacts_dir / "browser-installation-result.json").read_text(encoding="utf-8"))
    assert observed["phase"] == "complete" and observed["pageErrors"] == []
    artifact = observed["operation"]["artifact"]
    assert artifact["installed_distributions"] == {"artifact-server": "1.0.0", "locked-leaf": "1.0.0"}
    assert Path(artifact["command"]).is_file()
    assert (case.workspace / ".boxteam/workspace.jsonc").read_bytes() == original
    assert case.client.get("/api/v1/mcp/installations").json()["data"] == [artifact]
    assert case.client.get("/api/v1/mcp/servers").json()["data"] == []
