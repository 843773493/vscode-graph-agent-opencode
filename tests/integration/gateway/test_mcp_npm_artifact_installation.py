"""真实 npm registry／Gateway／Backend／source CAS／原 Thread 的受管启动。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import quote, unquote, urlsplit

import commentjson
import pytest

from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 同会话正式构建和消费收据
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    InstallationCase,
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    installation_case as installation_case,  # noqa: PLC0414 - 复用真实 Gateway／Backend 与统一模型回放
)
from tests.support.http_server import running_http_server
from tests.support.paths import chromium_temporary_environment

_SERVER = """import {createInterface} from 'node:readline';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import leaf from 'probe-leaf';
import parent from 'probe-parent';
const lines = createInterface({input:process.stdin});
for await (const line of lines) {
  const request = JSON.parse(line);
  if (request.id === undefined) continue;
  let result;
  if (request.method === 'initialize') {
    result = {protocolVersion:request.params.protocolVersion,capabilities:{tools:{}},serverInfo:{name:'npm-probe',version:'1.0.0'}};
  } else if (request.method === 'tools/list') {
    result = {tools:[{name:'installation_probe',description:'读取实际 npm 制品与运行路径',inputSchema:{type:'object',properties:{}}}]};
  } else if (request.method === 'tools/call') {
    if (request.params.name !== 'installation_probe') throw Error('未知测试工具');
    result = {content:[{type:'text',text:JSON.stringify({executable:process.execPath,entrypoint:fileURLToPath(import.meta.url),
      cwd:process.cwd(),leaf,parent,secret_sha256:createHash('sha256').update(process.env.PROBE_SECRET ?? '').digest('hex')})}]};
  } else if (request.method === 'ping') {
    result = {};
  } else {
    throw Error('未知测试协议方法: '+request.method);
  }
  process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:request.id,result})+'\\n');
}
"""


@dataclass
class NpmIndex:
    root: Path
    url: str = ""
    requests: list[str] = field(default_factory=list)
    packages: dict[str, dict[str, dict[str, object]]] = field(default_factory=dict)
    tarballs: dict[str, bytes] = field(default_factory=dict)
    manifests: dict[str, dict[str, object]] = field(default_factory=dict)
    downloading: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    download_cancelled: threading.Event = field(default_factory=threading.Event)

    def publish(self, name: str, version: str, code: str, *, dependencies: dict[str, str] | None = None,
        entrypoint: bool = False, scripts: dict[str, str] | None = None) -> None:
        """生成标准 registry 元数据和 tgz；解析、锁定及安装都交给真实 npm。"""
        package: dict[str, object] = {"name": name, "version": version, "type": "module", "main": "index.mjs"}
        if dependencies:
            package["dependencies"] = dependencies
        if entrypoint:
            package["bin"] = {"npm-probe": "index.mjs"}
        if scripts:
            package["scripts"] = scripts
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for filename, data in {"package.json": json.dumps(package).encode(), "index.mjs": code.encode()}.items():
                info = tarfile.TarInfo("package/" + filename)
                info.size, info.mode = len(data), 0o755 if filename.endswith(".mjs") else 0o644
                archive.addfile(info, io.BytesIO(data))
        data = buffer.getvalue()
        path = "/" + quote(name, safe="") + "/-/" + name.rsplit("/", 1)[-1] + "-" + version + ".tgz"
        self.tarballs[path] = data
        metadata = {**package, "dist": {"tarball": self.url + path, "shasum": hashlib.sha1(data).hexdigest(),
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()}}
        self.packages.setdefault(name, {})[version] = metadata
        (self.root / (name.replace("/", "_") + "-" + version + ".tgz")).write_bytes(data)

    def source(self, release: str = "release-1", package_version: str = "1.0.0") -> dict[str, str]:
        if release not in self.manifests:
            package: dict[str, object] = {"registryType": "npm", "identifier": "npm-artifact-server", "version": package_version,
                "registryBaseUrl": self.url, "transport": {"type": "stdio"}}
            if release == "release-1":
                package["environmentVariables"] = [{"name": "PROBE_SECRET", "isSecret": True, "isRequired": True}]
            self.manifests[release] = {"server": {"name": "org.example/npm", "title": "Artifact Server", "description": "标准 npm 锁定安装验证",
                "version": release, "packages": [package]}, "_meta": {}}
        return {"kind": "gallery", "name": "org.example/npm", "version": release, "endpoint": self.url,
            "entry_url": self.url + "/v0.1/servers/org.example%2Fnpm/versions/" + release}


@pytest.fixture
def npm_index(request: pytest.FixtureRequest) -> Iterator[NpmIndex]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = context.artifacts_dir / request.node.name / "registry"
    root.mkdir(parents=True, exist_ok=True)
    index = NpmIndex(root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *arguments: object) -> None:
            index.requests.append(format % arguments)

        def end_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path.endswith("npm-artifact-server-4.0.0.tgz"):
                index.downloading.set()
                assert index.release.wait(40), "测试未释放原 npm tarball 下载"
                if index.download_cancelled.is_set():
                    return  # 原 npm 已确认退出，不再向已关闭的请求写入响应。
            if path == "/v0.1/servers":
                data = json.dumps({"servers": [index.manifests["release-1"]], "metadata": {"count": 1}}).encode()
                content_type = "application/json"
            elif path in index.tarballs:
                data, content_type = index.tarballs[path], "application/octet-stream"
            else:
                name = unquote(path).removeprefix("/")
                release = name.removeprefix("v0.1/servers/org.example/npm/versions/")
                if release in index.manifests:
                    document = index.manifests[release]
                elif name in index.packages:
                    versions = index.packages[name]
                    document = {"name": name, "dist-tags": {"latest": next(reversed(versions))}, "versions": versions}
                else:
                    self.send_error(404)
                    return
                data, content_type = json.dumps(document).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as server:
        index.url = f"http://127.0.0.1:{server.server_port}"
        index.publish("probe-leaf", "1.0.0", "export default 'locked-one';\n")
        index.publish("probe-leaf", "2.0.0", "export default 'nested-two';\n")
        index.publish("probe-parent", "1.0.0", "import leaf from 'probe-leaf';export default leaf;\n", dependencies={"probe-leaf": "2.0.0"})
        dependencies = {"probe-leaf": "^1.0.0", "probe-parent": "1.0.0"}
        index.publish("npm-artifact-server", "1.0.0", _SERVER, dependencies=dependencies, entrypoint=True)
        index.publish("npm-artifact-server", "3.0.0", _SERVER, dependencies=dependencies, entrypoint=True,
            scripts={"postinstall": 'node --input-type=module -e "import {writeFileSync} from \'node:fs\';writeFileSync(\'install-ran\',\'unexpected\')"'})
        index.publish("npm-artifact-server", "4.0.0", _SERVER, dependencies=dependencies, entrypoint=True)
        index.source()
        try:
            yield index
        finally:
            index.release.set()
            (root.parent / "registry-requests.json").write_text(json.dumps(index.requests, indent=2), encoding="utf-8")


def test_locked_npm_generation_uses_original_node_and_thread_after_failed_or_cancelled_installs(
    installation_case: InstallationCase, npm_index: NpmIndex,
) -> None:
    case = installation_case
    evidence: list[dict[str, object]] = []

    def exchange(method: str, path: str, key: str, body: dict[str, object] | None = None, *, status: int = 200) -> dict:
        response = case.client.request(method, path, json=body, headers={"X-Request-ID": key})
        assert response.status_code == status, response.text
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"] == key
        evidence.append({"path": path, "method": method, "status": status, "response": payload})
        return payload

    workspace_id = exchange("GET", "/api/gateway/workspaces", "npm-workspaces")["data"]["items"][0]["workspace_id"]
    base = f"/api/v1/workspaces/{workspace_id}"
    capabilities = exchange("GET", base + "/mcp/installation-capabilities", "npm-capabilities")
    target = capabilities["target"]
    assert capabilities["data"]["npm_available"] is True
    assert "npm" not in capabilities["data"]["unsupported_registry_types"]
    workspace_target = {"kind": "workspace", "gateway_id": target["gateway_id"], "workspace_id": workspace_id}
    secret = "npm-private-" + uuid.uuid4().hex

    def candidate_for(release: str = "release-1", version: str = "1.0.0") -> tuple[dict, dict]:
        source = npm_index.source(release, version)
        inspected = exchange("POST", base + "/mcp/candidate-manifests", "npm-inspect-" + release, {"target": target, "source": source})["data"]
        candidate = {"target": target, "source": source, "manifest_digest": inspected["manifest_digest"],
            "variant": inspected["variants"][0]["ref"], "server_id": "installed"}
        if release == "release-1":
            reference = exchange("POST", base + "/mcp/candidate-plans/private-env", "npm-private-input", {
                "target": workspace_target, "server_id": "installed", "input_key": "PROBE_SECRET", "candidate": candidate, "value": secret})["data"]
            candidate["inputs"] = {"PROBE_SECRET": reference}
        plan = exchange("POST", base + "/mcp/candidate-plans", "npm-plan-" + release, candidate)["data"]
        assert plan["disposition"] == "requires_artifact" and plan["parameters"]["registry_type"] == "npm"
        return candidate, plan

    def start(candidate: dict, plan: dict, operation_id: str) -> dict:
        return exchange("POST", base + "/mcp/installations", "npm-start-" + operation_id, {"candidate": candidate,
            "plan_digest": plan["plan_digest"], "operation_id": operation_id, "entrypoint": "npm-probe"})["data"]

    def settle(operation: dict) -> dict:
        deadline = time.monotonic() + 40
        while operation["status"] == "running":
            assert time.monotonic() < deadline, operation
            time.sleep(.1)
            operation = exchange("GET", base + "/mcp/installation-operations/" + operation["operation_id"], "npm-observe-" + operation["operation_id"])["data"]
        return operation

    original_config = (case.workspace / ".boxteam/workspace.jsonc").read_bytes()
    candidate, plan = candidate_for()
    operation = settle(start(candidate, plan, "npm-original"))
    assert operation["status"] == "succeeded", operation
    artifact = operation["artifact"]
    command, node = Path(artifact["command"]), Path(artifact["runtime_executable"])
    assert command.is_relative_to(case.home / "artifacts/mcp" / target["backend_id"]) and command.is_file()
    root = command.parent.parent.parent
    lock_bytes = (root / "package-lock.json").read_bytes()
    lock = json.loads(lock_bytes)
    assert lock["lockfileVersion"] == 3 and lock["packages"][""]["dependencies"] == {"npm-artifact-server": "1.0.0"}
    assert artifact["lock_digest"] == "sha256:" + hashlib.sha256(lock_bytes).hexdigest()
    assert artifact["registry_type"] == "npm" and node.is_absolute() and node.is_file() and artifact["runtime_version"].startswith("v")
    assert artifact["installed_distributions"] == {"npm-artifact-server": "1.0.0", "probe-leaf": "1.0.0", "probe-parent": "1.0.0",
        "probe-parent/node_modules/probe-leaf": "2.0.0"}
    assert (case.workspace / ".boxteam/workspace.jsonc").read_bytes() == original_config
    assert exchange("GET", base + "/mcp/servers", "npm-unconfigured")["data"] == []

    source_path = base + "/config/sources/workspace_root_mutable_override"
    document = exchange("GET", source_path, "npm-source")["data"]
    configuration = commentjson.loads(document["jsonc"])
    configuration["mcp"] = {"servers": {"installed": {"enabled": True, "transport": "stdio", "managed_launch": artifact["binding"],
        "env": plan["parameters"]["env"]}}}
    edit = {"gateway_id": document["gateway_id"], "workspace_id": document["workspace_id"], "source_key": document["source_key"],
        "jsonc": json.dumps(configuration), "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
        "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
        "expected_active_digest": document["active_digest"], "idempotency_key": "npm-bind"}
    exchange("POST", source_path + "/preflight", "npm-preflight", edit)
    configured = exchange("PUT", source_path, "npm-configure", edit)["data"]
    server, = exchange("GET", base + "/mcp/servers", "npm-ready")["data"]
    assert server["status"] == "ready"
    ledger_path = case.workspace / ".boxteam/resources.json"
    original_resource = ExternalResourceLeaseLedger(state_path=ledger_path).get(server["connection_ref"])
    assert original_resource is not None and original_resource.process is not None
    assert original_resource.process.pid is not None and not original_resource.process.exit_verified
    session = exchange("POST", base + "/sessions", "npm-session", {"title": "npm 锁定制品原 Thread 调用"})["data"]
    results: dict[str, dict] = {}

    def invoke(phase: str) -> None:
        call_id = "npm-probe-" + phase
        replies = [{"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": "invoke_extension_tool",
            "arguments": json.dumps({"tool_name": "mcp__installed__installation_probe", "arguments": {}})}}]}, {"content": "已读取 npm 锁定制品。"}]
        calls = case.replay.control.plan_model_calls(session["session_id"], session["thread_id"], [cast(list[dict[str, object]], build_cassette(
            asset_id=f"npm-{phase}-{index}", model="e2e-stub-model", url="https://opencode.ai/zen/v1/chat/completions",
            frames=chat_completion_frames(response_id=f"npm-{phase}-{index}", model="e2e-stub-model", deltas=({"role": "assistant", **reply},),
                finish_reason="tool_calls" if "tool_calls" in reply else "stop")).raw["interactions"])[0] for index, reply in enumerate(replies)])
        accepted = exchange("POST", base + f"/sessions/{session['session_id']}/messages", "npm-message-" + phase, {
            "message": {"content": "调用已安装探针，核对原 Node、精确依赖和私有输入。"}, "run": {"mode": "single_agent", "agent_id": "default"}})["data"]
        deadline = time.monotonic() + 40
        while True:
            job = exchange("GET", base + f"/jobs/{accepted['job_id']}", "npm-job-" + phase)["data"]
            if job["status"] in {"succeeded", "completed"}:
                break
            assert job["status"] not in {"failed", "cancelled", "timed_out"} and time.monotonic() < deadline, job
            time.sleep(.1)
        wire = json.loads((case.replay.artifacts / f"{calls[1]}.json").read_text(encoding="utf-8"))["request"]
        messages = {message["tool_call_id"]: message for message in wire["messages"] if message["role"] == "tool"}
        result = messages[call_id]
        assert result["tool_call_id"] == call_id and result["name"] == "invoke_extension_tool"
        part, = result["content"]
        assert part["type"] == "text" and json.loads(part["text"]) == {"executable": str(node), "entrypoint": str(command),
            "cwd": str(case.workspace), "leaf": "locked-one", "parent": "nested-two", "secret_sha256": hashlib.sha256(secret.encode()).hexdigest()}
        assert all(messages[previous_id] == previous for previous_id, previous in results.items())
        results[call_id] = result
        evidence.append({"phase": phase, "session_id": session["session_id"], "thread_id": session["thread_id"], "model_call_ids": calls,
            "tool_results": messages})

    invoke("original")
    npm_index.publish("probe-leaf", "1.1.0", "export default 'changed-index';\n")
    previous_downloads = list(npm_index.requests)
    reused = settle(start(candidate, plan, "npm-reuse"))
    assert reused["status"] == "succeeded" and reused["artifact"] == artifact
    assert not any(".tgz" in line for line in npm_index.requests[len(previous_downloads):])
    assert (root / "package-lock.json").read_bytes() == lock_bytes

    missing_candidate, missing_plan = candidate_for("release-missing", "2.0.0")
    missing = settle(start(missing_candidate, missing_plan, "npm-missing"))
    assert missing["status"] == "failed" and missing["stage"] == "lock" and missing["artifact"] is None, missing
    script_candidate, script_plan = candidate_for("release-script", "3.0.0")
    scripted = settle(start(script_candidate, script_plan, "npm-script"))
    assert scripted["status"] == "failed" and scripted["artifact"] is None and "install scripts" in scripted["error"], scripted
    assert not list(case.home.rglob("install-ran"))
    cancel_candidate, cancel_plan = candidate_for("release-cancel", "4.0.0")
    first = start(cancel_candidate, cancel_plan, "npm-cancel-one")
    second = start(cancel_candidate, cancel_plan, "npm-cancel-two")
    assert npm_index.downloading.wait(20), "未观察到真实 npm tarball 下载"
    exchange("POST", base + "/mcp/installation-operations/npm-cancel-one/cancel", "npm-cancel-first", target)
    first = settle(first)
    assert first["status"] == "cancelled" and first["artifact"] is None
    assert exchange("GET", base + "/mcp/installation-operations/npm-cancel-two", "npm-shared-still-running")["data"]["status"] == "running"
    exchange("POST", base + "/mcp/installation-operations/npm-cancel-two/cancel", "npm-cancel-last", target)
    second = settle(second)
    assert second["status"] == "cancelled" and second["artifact"] is None
    npm_index.download_cancelled.set()
    npm_index.release.set()

    assert exchange("GET", base + "/mcp/installations", "npm-only-original")["data"] == [artifact]
    assert exchange("GET", base + "/mcp/servers", "npm-original-connection")["data"] == [server]
    current = exchange("GET", source_path, "npm-original-source")["data"]
    for field_name in ("file_digest", "layer_revision", "layer_digest", "active_revision", "active_digest", "effective"):
        assert current[field_name] == configured[field_name], field_name
    resource = ExternalResourceLeaseLedger(state_path=ledger_path).get(server["connection_ref"])
    assert resource is not None and resource.process == original_resource.process and resource.status == "running"
    assert resource.managed_launch is not None and resource.managed_launch.model_dump() == artifact["binding"]
    assert not resource.cleanup_failed and (root / "package-lock.json").read_bytes() == lock_bytes
    references = exchange("GET", base + f"/mcp/installations/{artifact['binding']['installation_id']}/references", "npm-references")["data"]
    assert {item["kind"] for item in references["references"]} >= {"source_layer", "active", "connection"}
    rejected = exchange("DELETE", base + f"/mcp/installations/{artifact['binding']['installation_id']}", "npm-reference-blocked", {"target": target,
        "binding": artifact["binding"], "operation_id": "npm-blocked-remove"}, status=409)
    assert rejected["data"]["binding"] == artifact["binding"] and command.is_file()
    invoke("after-failure-and-cancel")
    encoded = json.dumps({"http": evidence, "artifact": artifact, "tool_results": results}, ensure_ascii=False, indent=2)
    assert secret not in encoded and secret not in (case.workspace / ".boxteam/workspace.jsonc").read_text(encoding="utf-8")
    (case.context.artifacts_dir / "npm-installation-evidence.json").write_text(encoded, encoding="utf-8")


def test_workbench_npm_install_action_uses_explicit_root_bin_without_configuring(
    installation_case: InstallationCase, npm_index: NpmIndex,
) -> None:
    case = installation_case
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "npm 安装 UI 验收需要真实 Chromium"
    # 安装 UI 只发布制品；私有输入已由上面的真实候选／配置／Thread 场景单独核实。
    package, = cast(list[dict[str, object]], cast(dict[str, object], npm_index.manifests["release-1"]["server"])["packages"])
    del package["environmentVariables"]
    original = (case.workspace / ".boxteam/workspace.jsonc").read_bytes()
    artifacts = case.context.artifacts_dir / "browser-npm"
    artifacts.mkdir(parents=True, exist_ok=True)
    command = ["bun", "tests/integration/clients/web/mcp_artifact_installation.mjs"]
    protected = ["timeout", "150", "bash", "-c", 'ulimit -d 4194304; exec "$@"', "bash", *command]
    with chromium_temporary_environment(artifacts / "chromium-tmp") as temporary:
        result = subprocess.run(protected, timeout=165, check=False, capture_output=True, text=True, env={**os.environ, **temporary,
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable, "BOXTEAM_ARTIFACT_FIXTURE": json.dumps({
                "baseUrl": str(case.client.base_url).rstrip("/"), "registryUrl": npm_index.url, "artifacts": str(artifacts),
                "testFile": "tests/integration/gateway/test_mcp_npm_artifact_installation.py", "registryType": "npm",
                "identifier": "npm-artifact-server", "entrypoint": "npm-probe", "title": "Artifact Server"})})
    (artifacts / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (artifacts / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    (artifacts / "browser-process.json").write_text(json.dumps({"command": command, "protected_command": protected,
        "exit_code": result.returncode}, indent=2), encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((artifacts / "browser-installation-result.json").read_text(encoding="utf-8"))
    assert observed["phase"] == "complete" and observed["pageErrors"] == []
    artifact = observed["operation"]["artifact"]
    assert artifact["registry_type"] == "npm" and artifact["entrypoint"] == "npm-probe"
    assert artifact["installed_distributions"] == {"npm-artifact-server": "1.0.0", "probe-leaf": "1.0.0", "probe-parent": "1.0.0",
        "probe-parent/node_modules/probe-leaf": "2.0.0"}
    assert Path(artifact["command"]).is_file() and Path(artifact["runtime_executable"]).is_absolute()
    assert artifact["runtime_version"].startswith("v")
    assert (case.workspace / ".boxteam/workspace.jsonc").read_bytes() == original
    assert case.client.get("/api/v1/mcp/installations").json()["data"] == [artifact]
    assert case.client.get("/api/v1/mcp/servers").json()["data"] == []
