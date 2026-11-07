"""真实双 Backend 的冷态归并和暖复用；各工作区仍沿原 MCP owner 独立连接。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import commentjson
import httpx
import psutil
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_mcp_artifact_installation import (
    _wheel,
    installation_route,
)
from tests.support.http_server import running_http_server
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


@dataclass
class WheelGate:
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    requests: list[str] = field(default_factory=list)
    blocked: bool = False
    fail: bool = False


@dataclass(frozen=True)
class ReuseCase:
    client: httpx.Client
    artifacts_dir: Path
    workspaces: tuple[Path, Path]
    home: Path
    index: str
    requests: list[str]
    gate: WheelGate


@pytest.fixture
def reuse_case(request: pytest.FixtureRequest) -> Iterator[ReuseCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir / request.node.name
    artifacts.mkdir(parents=True, exist_ok=True)
    workspaces = tuple(prepare_test_workspace(workspace_root=context.workspace_root / request.node.name / name,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=()) for name in ("A1", "A2"))
    for workspace in workspaces:
        install_test_workspace_config(workspace_root=workspace, config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
            schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    home = context.boxteam_home_for_node(request.node.nodeid)
    index_root = artifacts / "index"
    _wheel(index_root, "locked-leaf", "1.0.0", b"VALUE = 'locked-one'\n")
    _wheel(index_root, "artifact-server", "1.0.0", (Path.cwd() / "tests/integration/workspace_services/mcp/artifact_server.py").read_bytes(),
        requires="Requires-Dist: locked-leaf>=1\n", entrypoint="artifact-server")
    requests: list[str] = []
    gate = WheelGate()

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, format: str, *arguments: object) -> None:
            requests.append(format % arguments)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path.startswith("/simple/artifact-server/") and path.endswith(".whl"):
                gate.requests.append(path)
                if gate.blocked:
                    gate.entered.set()
                    if not gate.release.wait(20):
                        self.send_error(504, "test did not release original wheel response")
                        return
                if gate.fail:
                    # 明确不可重试的原来源失败，不能改投其它 index 或返回假成功。
                    self.send_error(403, "cold-producer-download-refused")
                    return
            super().do_GET()

    try:
        with running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(index_root)))) as index:
            gateway = start_gateway_process(workspace_root=workspaces[0], default_backend_url=None,
                port=integration_port_block_for_file(Path(request.node.path)).port(0), boxteam_home=home, artifacts_dir=artifacts)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30, trust_env=False) as client:
                    client.post("/api/gateway/users/guest", json={"tracking": {"test": True}}).raise_for_status()
                    yield ReuseCase(client, artifacts, (workspaces[0], workspaces[1]), home,
                        f"http://127.0.0.1:{index.server_port}/simple", requests, gate)
            finally:
                gate.release.set()
                close_gateway_process(gateway)
    finally:
        (artifacts / "index-requests.json").write_text(json.dumps({"http": requests, "wheel_gets": gate.requests}, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("mode", ["warm", "cold", "cold-failure"])
def test_shared_artifact_preserves_generation_and_independent_workspace_connections(reuse_case: ReuseCase, mode: str) -> None:
    case = reuse_case
    target, installation_base = installation_route(case.client)
    evidence: list[dict[str, object]] = []
    native_processes: list[dict[str, object]] = []
    prepare_processes: list[dict[str, object]] = []

    def exchange(method: str, path: str, key: str, body: dict[str, object] | None = None) -> dict:
        response = case.client.request(method, path, json=body, headers={"X-Request-ID": key, "X-BoxTeam-Gateway-Id": target["gateway_id"]})
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"] == key
        evidence.append({"method": method, "path": path, "request_id": key, "status": response.status_code, "body": payload})
        assert response.status_code == 200, response.text
        return payload

    def completed(base: str, operation_id: str, status: str = "succeeded") -> dict:
        deadline = time.monotonic() + 40
        operation = exchange("GET", base + "/installation-operations/" + operation_id, operation_id + "-observe")["data"]
        while operation["status"] == "running":
            assert time.monotonic() < deadline, operation
            time.sleep(0.1)
            operation = exchange("GET", base + "/installation-operations/" + operation_id, operation_id + "-observe")["data"]
        assert operation["status"] == status, operation
        return operation

    def configure(base: str, label: str, binding: dict) -> dict:
        path = base + "/config/sources/workspace_root_mutable_override"
        document = exchange("GET", path, label + "-source")["data"]
        config = commentjson.loads(document["jsonc"])
        config["mcp"] = {"servers": {"installed": {"enabled": True, "transport": "stdio", "managed_launch": binding,
            "args": [label], "env": {"MCP_WORKSPACE_MARKER": label}}}}
        edit = {"gateway_id": document["gateway_id"], "workspace_id": document["workspace_id"], "source_key": document["source_key"],
            "jsonc": json.dumps(config), "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
            "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
            "expected_active_digest": document["active_digest"], "idempotency_key": label + "-use-artifact"}
        exchange("POST", path + "/preflight", label + "-preflight", edit)
        exchange("PUT", path, label + "-configure", edit)
        result = exchange("GET", base + "/mcp/servers", label + "-servers")
        server, = result["data"]
        assert server["status"] == "ready" and len(server["tools"]) == 1 and server["connection_ref"]
        assert result["target"]["workspace_id"] == document["workspace_id"]
        return server

    try:
        first_id = exchange("GET", "/api/gateway/workspaces", "reuse-workspaces")["data"]["items"][0]["workspace_id"]
        first_base = f"/api/v1/workspaces/{first_id}"
        exchange("GET", first_base + "/workspace", "reuse-A1-workspace")
        target = exchange("GET", installation_base + "/installation-capabilities", "reuse-A1-capabilities")["target"]
        listing = exchange("POST", "/api/gateway/managed-workspaces", "reuse-register-A2",
            {"root_path": str(case.workspaces[1]), "name": "共享制品 A2"})["data"]
        second_id = next(item["workspace_id"] for item in listing["items"] if item["workspace_id"] != first_id)
        second_base = f"/api/v1/workspaces/{second_id}"
        # 安装域入口不选择工作区；跨进程验收明确调用第二 Backend 的同一私有 HTTP 入口。
        second_backend = next(item for item in exchange("GET", "/api/gateway/workspaces", "reuse-backend-routes")["data"]["items"] if item["workspace_id"] == second_id)
        second_installation_base = second_backend["backend_url"].rstrip("/") + "/api/v1/mcp"
        exchange("GET", second_base + "/workspace", "reuse-A2-workspace")
        assert exchange("GET", second_installation_base + "/installation-capabilities", "reuse-A2-capabilities")["target"] == target
        healths: list[dict] = []
        for label, base in (("A1", first_base), ("A2", second_base)):
            key = "reuse-" + label + "-process"
            response = case.client.get(base + "/health", headers={"X-Request-ID": key})
            response.raise_for_status()
            assert response.headers["X-Request-ID"] == key
            health = response.json()
            evidence.append({"method": "GET", "path": base + "/health", "request_id": key, "status": response.status_code, "body": health})
            assert health["status"] == "ok" and health["backend_id"] == target["backend_id"]
            healths.append(health)
        assert healths[0]["process_id"] != healths[1]["process_id"]
        source = {"kind": "manual", "name": "org.example/artifact-reuse", "version": "1", "definition": {"packages": [
            {"registryType": "pypi", "identifier": "artifact-server", "version": "1.0.0", "registryBaseUrl": case.index,
             "transport": {"type": "stdio"}, "packageArguments": [{"type": "positional", "valueHint": "workspace-argument", "isRequired": True}],
             "environmentVariables": [{"name": "MCP_WORKSPACE_MARKER", "isRequired": True}]}]}}
        inspected = exchange("POST", installation_base + "/candidate-manifests", "reuse-inspect", {"target": target, "source": source})["data"]

        def install_intent(label: str) -> dict[str, object]:
            candidate = {"target": target, "source": source, "manifest_digest": inspected["manifest_digest"], "variant": inspected["variants"][0]["ref"],
                "inputs": {}}
            plan = exchange("POST", installation_base + "/candidate-plans", label + "-plan", candidate)["data"]
            assert plan["parameters"]["arguments"] == [] and plan["parameters"]["env"] == {}
            return {"candidate": candidate, "plan_digest": plan["plan_digest"], "operation_id": "install-" + label, "entrypoint": "artifact-server"}

        first_intent, second_intent = install_intent("A1"), install_intent("A2")
        assert first_intent["plan_digest"] == second_intent["plan_digest"] and case.requests == []
        first_config, second_config = [(workspace / ".boxteam/workspace.jsonc").read_bytes() for workspace in case.workspaces]
        installation_root = case.home / "artifacts/mcp" / target["backend_id"]
        expected_operations, expected_generations = 2, 1
        case.gate.blocked, case.gate.fail = mode != "warm", mode == "cold-failure"
        exchange("POST", installation_base + "/installations", "reuse-install-A1", first_intent)
        if mode == "warm":
            original = completed(installation_base, "install-A1")
            requests_before_reuse = list(case.requests)
            exchange("POST", second_installation_base + "/installations", "reuse-install-A2", second_intent)
            reused = completed(second_installation_base, "install-A2")
            assert case.requests == requests_before_reuse and "复用原 generation" in reused["log"]
        else:
            assert case.gate.entered.wait(10), "原 uv 未进入真实 wheel 下载响应屏障"
            accepted = exchange("POST", second_installation_base + "/installations", "reuse-install-A2", second_intent)["data"]
            assert accepted["status"] == "running" and accepted["artifact"] is None
            with sqlite3.connect((installation_root / "installations.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
                assert connection.execute("SELECT COUNT(*) FROM operations").fetchone() == (2,)
                assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (0,)
                assert connection.execute("SELECT operation_id, pid FROM operation_owners").fetchall() == [("install-A1", healths[0]["process_id"])]
                producer_id, installation_id, generation = connection.execute("SELECT operation_id, installation_id, generation FROM artifact_preparations").fetchone()
                assert producer_id == "install-A1"
                assert connection.execute("SELECT producer_operation_id FROM operations").fetchall() == [("install-A1",)] * 2
            generation_root = installation_root / "generations" / installation_id / generation
            assert generation_root.is_dir() and len(case.gate.requests) == 1
            for health in healths:
                for process in psutil.Process(health["process_id"]).children(recursive=True):
                    command = process.cmdline()
                    if str(generation_root) in command and "lock" in command:
                        prepare_processes.append({"backend_pid": health["process_id"], "pid": process.pid, "args": command})
            assert len(prepare_processes) == 1 and prepare_processes[0]["backend_pid"] == healths[0]["process_id"]
            case.gate.release.set()
            status = "failed" if mode == "cold-failure" else "succeeded"
            original, reused = completed(installation_base, "install-A1", status), completed(second_installation_base, "install-A2", status)
            assert original["stage"] == reused["stage"] and original["log"] == reused["log"] and original["error"] == reused["error"]
            if mode == "cold-failure":
                failed_original, failed_follower = original, reused
                assert original["artifact"] is reused["artifact"] is None and "阶段 lock 失败" in original["error"]
                requests_before_retry = list(case.requests)
                assert exchange("POST", installation_base + "/installations", "reuse-failed-A1-retry", first_intent)["data"] == failed_original
                assert exchange("POST", second_installation_base + "/installations", "reuse-failed-A2-retry", second_intent)["data"] == failed_follower
                assert case.requests == requests_before_retry and generation_root.is_dir()
                with sqlite3.connect((installation_root / "installations.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
                    assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (0,)
                    assert connection.execute("SELECT COUNT(*) FROM artifact_preparations").fetchone() == (0,)
                case.gate.blocked = case.gate.fail = False
                first_intent = {**first_intent, "operation_id": "recovered-A1"}
                second_intent = {**second_intent, "operation_id": "recovered-A2"}
                exchange("POST", installation_base + "/installations", "reuse-recover-A1", first_intent)
                original = completed(installation_base, "recovered-A1")
                requests_before_reuse = list(case.requests)
                exchange("POST", second_installation_base + "/installations", "reuse-recover-A2", second_intent)
                reused = completed(second_installation_base, "recovered-A2")
                assert case.requests == requests_before_reuse
                assert exchange("GET", installation_base + "/installation-operations/install-A1", "reuse-original-failure")["data"] == failed_original
                assert exchange("GET", installation_base + "/installation-operations/install-A2", "reuse-follower-failure")["data"] == failed_follower
                expected_operations, expected_generations = 4, 2
        artifact = original["artifact"]
        assert artifact["installed_distributions"] == {"artifact-server": "1.0.0", "locked-leaf": "1.0.0"}
        assert reused["artifact"] == artifact and reused["operation_id"] != original["operation_id"] and reused["intent_digest"] == original["intent_digest"]
        requests_after_install = list(case.requests)
        assert requests_after_install
        assert (case.workspaces[0] / ".boxteam/workspace.jsonc").read_bytes() == first_config
        assert (case.workspaces[1] / ".boxteam/workspace.jsonc").read_bytes() == second_config
        assert exchange("GET", installation_base + "/installation-operations/" + original["operation_id"], "reuse-original-operation")["data"] == original
        assert exchange("GET", installation_base + "/installations", "reuse-one-artifact")["data"] == [artifact]
        assert len(list((installation_root / "generations").glob("*/*"))) == expected_generations
        with sqlite3.connect((installation_root / "installations.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM operations").fetchone() == (expected_operations,)
            assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (1,)
            assert connection.execute("SELECT COUNT(*) FROM artifact_preparations").fetchone() == (0,)
        first_server, second_server = configure(first_base, "A1", artifact["binding"]), configure(second_base, "A2", artifact["binding"])
        assert second_server["connection_ref"] != first_server["connection_ref"]
        assert exchange("GET", first_base + "/mcp/servers", "reuse-A1-still-ready")["data"] == [first_server]
        for process in psutil.process_iter(["pid", "cmdline"]):
            command = process.info["cmdline"] or []
            if artifact["command"] in command:
                native_processes.append({"pid": process.pid, "cwd": process.cwd(), "args": command,
                    "marker": process.environ().get("MCP_WORKSPACE_MARKER")})
        assert {item["marker"] for item in native_processes} == {"A1", "A2"}
        for label, workspace in zip(("A1", "A2"), case.workspaces, strict=True):
            process, = [item for item in native_processes if item["marker"] == label]
            arguments = process["args"]
            assert process["cwd"] == str(workspace) and isinstance(arguments, list) and label in arguments
        disconnected = exchange("POST", first_base + "/mcp/connection-operations", "reuse-disconnect-A1",
            {"operation_id": "disconnect-A1", "server_id": "installed", "action": "disconnect"})["data"]
        deadline = time.monotonic() + 10
        while disconnected["status"] in {"pending", "verifying"}:
            assert time.monotonic() < deadline, disconnected
            disconnected = exchange("GET", first_base + "/mcp/connection-operations/disconnect-A1?wait_timeout_ms=1000", "reuse-disconnect-observe")["data"]
        assert disconnected["status"] == "succeeded", disconnected
        assert exchange("GET", second_base + "/mcp/servers", "reuse-A2-after-A1-disconnect")["data"] == [second_server]
        assert Path(artifact["command"]).is_file() and case.requests == requests_after_install
    finally:
        case.gate.release.set()
        (case.artifacts_dir / "artifact-reuse.json").write_text(json.dumps({"mode": mode, "http": evidence,
            "native_processes": native_processes, "prepare_processes": prepare_processes}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
