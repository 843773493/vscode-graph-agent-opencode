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
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import commentjson
import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式构建由同一会话复用
)
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

    def source(self) -> dict[str, str]:
        return {"kind": "gallery", "name": "org.example/artifact", "version": "release-1", "endpoint": self.url,
            "entry_url": self.url + "/v0.1/servers/org.example%2Fartifact/versions/release-1"}


@pytest.fixture
def artifact_index(request: pytest.FixtureRequest) -> Iterator[ArtifactIndex]:
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
            if urlsplit(self.path).path != "/v0.1/servers":
                super().do_GET()
                return
            document = json.loads(manifest.read_text(encoding="utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"servers": [document], "metadata": {"count": 1}}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    index = ArtifactIndex(root, f"http://127.0.0.1:{server.server_port}", requests)
    manifest = root / "v0.1/servers/org.example/artifact/versions/release-1"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"server": {"name": "org.example/artifact", "title": "Artifact Server", "description": "标准 wheel 安装验证", "version": "release-1", "packages": [
        {"registryType": "pypi", "identifier": "artifact-server", "version": "1.0.0", "registryBaseUrl": index.url + "/simple",
         "transport": {"type": "stdio"}}]}, "_meta": {}}), encoding="utf-8")
    try:
        yield index
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()
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
    config["agents"]["default"]["model"] = {"primary_provider": "primary", "fallback_providers": []}
    config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
    runtime = context.runtime_root_for_node(request.node.nodeid)
    home = context.boxteam_home_for_node(request.node.nodeid)
    port = integration_port_block_for_file(Path(request.node.path)).port(0)
    with model_stream_replay(context.output_root, runtime_root=runtime, business_assertion=request.node.nodeid) as replay:
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
