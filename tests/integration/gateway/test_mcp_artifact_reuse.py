"""真实 PyPI 发布后跨工作区暖复用；连接仍由各工作区原 MCP owner 管理。"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import commentjson
import httpx
import psutil
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_mcp_artifact_installation import _wheel
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


@dataclass(frozen=True)
class ReuseCase:
    client: httpx.Client
    context: TestRunContext
    workspaces: tuple[Path, Path]
    home: Path
    index: str
    requests: list[str]


@pytest.fixture
def reuse_case(request: pytest.FixtureRequest) -> Iterator[ReuseCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspaces = tuple(prepare_test_workspace(workspace_root=context.workspace_root / request.node.name / name,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace", template_items=()) for name in ("A1", "A2"))
    for workspace in workspaces:
        install_test_workspace_config(workspace_root=workspace, config_path=Path.cwd() / "configs/tests/workspace/default.jsonc",
            schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    home = context.boxteam_home_for_node(request.node.nodeid)
    index_root = context.artifacts_dir / "index"
    _wheel(index_root, "locked-leaf", "1.0.0", b"VALUE = 'locked-one'\n")
    _wheel(index_root, "artifact-server", "1.0.0", (Path.cwd() / "tests/integration/workspace_services/mcp/artifact_server.py").read_bytes(),
        requires="Requires-Dist: locked-leaf>=1\n", entrypoint="artifact-server")
    requests: list[str] = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, format: str, *arguments: object) -> None:
            requests.append(format % arguments)

    try:
        with running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(index_root)))) as index:
            gateway = start_gateway_process(workspace_root=workspaces[0], default_backend_url=None,
                port=integration_port_block_for_file(Path(request.node.path)).port(0), boxteam_home=home, artifacts_dir=context.artifacts_dir)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30, trust_env=False) as client:
                    client.post("/api/gateway/users/guest", json={"tracking": {"test": True}}).raise_for_status()
                    yield ReuseCase(client, context, (workspaces[0], workspaces[1]), home, f"http://127.0.0.1:{index.server_port}/simple", requests)
            finally:
                close_gateway_process(gateway)
    finally:
        (context.artifacts_dir / "index-requests.json").write_text(json.dumps(requests, indent=2) + "\n", encoding="utf-8")


def test_warm_reuse_preserves_generation_and_independent_workspace_connections(reuse_case: ReuseCase) -> None:
    case = reuse_case
    evidence: list[dict[str, object]] = []
    native_processes: list[dict[str, object]] = []

    def exchange(method: str, path: str, key: str, body: dict[str, object] | None = None) -> dict:
        response = case.client.request(method, path, json=body, headers={"X-Request-ID": key})
        payload = response.json()
        assert response.headers["X-Request-ID"] == payload["request_id"] == key
        evidence.append({"method": method, "path": path, "request_id": key, "status": response.status_code, "body": payload})
        assert response.status_code == 200, response.text
        return payload

    def completed(base: str, operation_id: str) -> dict:
        deadline = time.monotonic() + 40
        operation = exchange("GET", base + "/mcp/installation-operations/" + operation_id, operation_id + "-observe")["data"]
        while operation["status"] == "running":
            assert time.monotonic() < deadline, operation
            time.sleep(0.1)
            operation = exchange("GET", base + "/mcp/installation-operations/" + operation_id, operation_id + "-observe")["data"]
        assert operation["status"] == "succeeded", operation
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
        target = exchange("GET", first_base + "/mcp/installation-capabilities", "reuse-A1-capabilities")["target"]
        source = {"kind": "manual", "name": "org.example/artifact-reuse", "version": "1", "definition": {"packages": [
            {"registryType": "pypi", "identifier": "artifact-server", "version": "1.0.0", "registryBaseUrl": case.index,
             "transport": {"type": "stdio"}, "packageArguments": [{"type": "positional", "valueHint": "workspace-argument", "isRequired": True}],
             "environmentVariables": [{"name": "MCP_WORKSPACE_MARKER", "isRequired": True}]}]}}
        inspected = exchange("POST", first_base + "/mcp/candidate-manifests", "reuse-inspect", {"target": target, "source": source})["data"]

        def install_intent(base: str, label: str) -> dict[str, object]:
            candidate = {"target": target, "source": source, "manifest_digest": inspected["manifest_digest"], "variant": inspected["variants"][0]["ref"],
                "inputs": {"workspace-argument": label, "MCP_WORKSPACE_MARKER": label}}
            plan = exchange("POST", base + "/mcp/candidate-plans", label + "-plan", candidate)["data"]
            assert plan["parameters"]["arguments"] == [label]
            return {"candidate": candidate, "plan_digest": plan["plan_digest"], "operation_id": "install-" + label, "entrypoint": "artifact-server"}

        first_intent = install_intent(first_base, "A1")
        first_config = (case.workspaces[0] / ".boxteam/workspace.jsonc").read_bytes()
        exchange("POST", first_base + "/mcp/installations", "reuse-install-A1", first_intent)
        original = completed(first_base, "install-A1")
        artifact = original["artifact"]
        assert artifact["installed_distributions"] == {"artifact-server": "1.0.0", "locked-leaf": "1.0.0"}
        assert (case.workspaces[0] / ".boxteam/workspace.jsonc").read_bytes() == first_config
        first_server = configure(first_base, "A1", artifact["binding"])
        listing = exchange("POST", "/api/gateway/managed-workspaces", "reuse-register-A2", {"root_path": str(case.workspaces[1]), "name": "共享制品 A2"})["data"]
        second_id = next(item["workspace_id"] for item in listing["items"] if item["workspace_id"] != first_id)
        second_base = f"/api/v1/workspaces/{second_id}"
        exchange("GET", second_base + "/workspace", "reuse-A2-workspace")
        assert exchange("GET", second_base + "/mcp/installation-capabilities", "reuse-A2-capabilities")["target"] == target
        second_intent = install_intent(second_base, "A2")
        assert second_intent["plan_digest"] != first_intent["plan_digest"]
        second_config = (case.workspaces[1] / ".boxteam/workspace.jsonc").read_bytes()
        requests_before_reuse = list(case.requests)
        exchange("POST", second_base + "/mcp/installations", "reuse-install-A2", second_intent)
        reused = completed(second_base, "install-A2")
        assert reused["artifact"] == artifact and reused["operation_id"] != original["operation_id"]
        assert reused["intent_digest"] != original["intent_digest"] and "复用原 generation" in reused["log"]
        assert case.requests == requests_before_reuse and requests_before_reuse
        assert (case.workspaces[1] / ".boxteam/workspace.jsonc").read_bytes() == second_config
        assert exchange("GET", first_base + "/mcp/installation-operations/install-A1", "reuse-original-operation")["data"] == original
        assert exchange("GET", second_base + "/mcp/installations", "reuse-one-artifact")["data"] == [artifact]
        installation_root = case.home / "artifacts/mcp" / target["backend_id"]
        assert len(list((installation_root / "generations").glob("*/*"))) == 1
        with sqlite3.connect((installation_root / "installations.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
            assert connection.execute("SELECT COUNT(*) FROM operations").fetchone() == (2,)
            assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone() == (1,)
        second_server = configure(second_base, "A2", artifact["binding"])
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
        assert Path(artifact["command"]).is_file() and case.requests == requests_before_reuse
    finally:
        (case.context.artifacts_dir / "warm-reuse.json").write_text(json.dumps({"http": evidence, "native_processes": native_processes}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
