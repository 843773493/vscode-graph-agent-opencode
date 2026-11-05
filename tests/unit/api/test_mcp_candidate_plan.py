"""实际候选 API 经唯一 owner 读取受控 HTTP 原件；不启动服务或执行安装。"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.api import mcp
from app.api.deps import get_mcp_catalog_owner, verify_local_token
from app.core.trace_middleware import TraceMiddleware
from app.schemas.internal_v2.mcp import McpTargetPlatformDTO
from app.services.infrastructure.events.event_channel_service import EventChannelService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner, candidate_source
from tests.harness.python.run_context import TestRunContext

GATEWAY_ID = "gateway_00000000000040008000000000000001"
BACKEND_ID = "00000000-0000-4000-8000-000000000011"
TARGET = {"kind": "backend", "gateway_id": GATEWAY_ID, "backend_id": BACKEND_ID}
BASE = "/api/v1/mcp"


@dataclass
class CandidateApi:
    client: TestClient
    root: Path
    raw: dict[str, object]
    http_client: type[httpx.AsyncClient]
    requests: list[httpx.Request] = field(default_factory=list)
    status: int = 200
    content: bytes | None = None

    def source(self, endpoint: str = "https://registry.test") -> dict[str, object]:
        return {"kind": "gallery", "name": "org.example/same", "version": "release-1", "endpoint": endpoint,
                "entry_url": endpoint + "/v0.1/servers/org.example%2Fsame/versions/release-1"}

    def inspect(self, source: dict[str, object] | None = None) -> dict[str, object]:
        response = self.client.post(BASE + "/candidate-manifests", json={"target": TARGET, "source": source or self.source()})
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def request(self, source: dict[str, object] | None = None, *, index: int = 0) -> dict[str, object]:
        source = source or self.source()
        document = self.inspect(source)
        variants = cast(list[dict[str, object]], document["variants"])
        return {"target": TARGET, "source": source, "manifest_digest": document["manifest_digest"],
                "variant": variants[index]["ref"]}


@pytest.fixture
def candidate_api(empty_workspace_scope: tuple[str, Path], monkeypatch: pytest.MonkeyPatch) -> Iterator[CandidateApi]:
    from app.main import app as workspace_app

    _, root = empty_workspace_scope
    package = {"registryType": "npm", "identifier": "@example/first", "version": "1.2.3", "transport": {"type": "stdio"}}
    raw: dict[str, object] = {"server": {"name": "org.example/same", "version": "release-1", "description": "test",
                                      "packages": [package, {**package, "identifier": "@example/second"}]}, "_meta": {}}
    owner = McpCatalogOwner(raw_config={"servers": {}}, workspace_root=root,
                            event_service=EventChannelService(), leases=ExternalResourceLeaseLedger())
    application = FastAPI()
    application.add_middleware(TraceMiddleware)
    application.include_router(mcp.router, prefix="/api/v1")
    application.state.backend_id = BACKEND_ID
    for error_type in (HTTPException, RequestValidationError):
        application.add_exception_handler(error_type, workspace_app.exception_handlers[error_type])
    application.dependency_overrides[verify_local_token] = lambda: "local"
    application.dependency_overrides[get_mcp_catalog_owner] = lambda: owner
    real_client = httpx.AsyncClient
    with TestClient(application, headers={"X-BoxTeam-Gateway-Id": GATEWAY_ID, "X-Request-ID": "candidate-api"}) as client:
        case = CandidateApi(client, root, raw, real_client)

        def respond(request: httpx.Request) -> httpx.Response:
            case.requests.append(request)
            return httpx.Response(case.status, json=case.raw) if case.content is None else httpx.Response(case.status, content=case.content)

        def source_client(*, timeout: float, trust_env: bool, follow_redirects: bool) -> httpx.AsyncClient:
            return real_client(transport=httpx.MockTransport(respond), timeout=timeout,
                               trust_env=trust_env, follow_redirects=follow_redirects)

        monkeypatch.setattr(candidate_source.httpx, "AsyncClient", source_client)
        before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        yield case
        after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        assert before == after
        assert not owner.started and owner._session_scope is None


@pytest.fixture
def candidate_http_registry(
    candidate_api: CandidateApi, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, list[str]]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    paths: list[str] = []
    served: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            paths.append(self.path)
            served.append(deepcopy(candidate_api.raw))
            content = json.dumps(candidate_api.raw).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    # 真实 socket 读取沿原 HTTPX reader，恢复 fixture 原客户端，不能仍命中 MockTransport。
    monkeypatch.setattr(candidate_source.httpx, "AsyncClient", candidate_api.http_client)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", paths
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "候选原件 HTTP 线程未退出"
        (context.artifacts_dir / "registry-requests-real-socket.json").write_text(
            json.dumps({"paths": paths, "served": served}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )


def test_actual_http_registry_manifest_is_reloaded_before_consuming_exact_variant(
    candidate_api: CandidateApi, candidate_http_registry: tuple[str, list[str]],
) -> None:
    endpoint, paths = candidate_http_registry
    source = candidate_api.source(endpoint)
    request = candidate_api.request(source, index=1)
    prepared = candidate_api.client.post(BASE + "/candidate-plans", json=request)
    assert prepared.status_code == 200, prepared.text
    plan = prepared.json()["data"]
    assert plan["source"]["protocol"] == "v0.1" and plan["source"]["entry_id"] is None
    assert plan["parameters"]["identifier"] == "@example/second" and plan["variant"]["index"] == 1
    assert plan["disposition"] == "requires_artifact" and "command" not in plan["parameters"]
    validated = candidate_api.client.post(BASE + "/candidate-plans/validate", json={"candidate": request, "plan_digest": plan["plan_digest"]})
    assert validated.status_code == 200 and validated.json()["data"] == plan
    server = cast(dict[str, object], candidate_api.raw["server"])
    packages = cast(list[dict[str, object]], server["packages"])
    packages.pop(1)
    rejected = candidate_api.client.post(BASE + "/candidate-plans/validate", json={"candidate": request, "plan_digest": plan["plan_digest"]})
    assert rejected.status_code == 409, rejected.text
    assert paths == [urlsplit(cast(str, source["entry_url"])).path] * 4
    assert not candidate_api.requests


def test_exact_second_package_is_planned_without_download_start_or_config_write(candidate_api: CandidateApi) -> None:
    request = candidate_api.request(index=1)
    response = candidate_api.client.post(BASE + "/candidate-plans", json=request)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target"] == TARGET and body["protocol_version"] == 1
    assert body["request_id"] == response.headers["X-Request-ID"] == "candidate-api"
    assert response.headers["Cache-Control"] == "no-store"
    plan = body["data"]
    assert plan["disposition"] == "requires_artifact" and plan["download_required"] is True
    assert plan["parameters"]["identifier"] == "@example/second" and "command" not in plan["parameters"]
    assert plan["platform"] == candidate_source.native_mcp_platform().model_dump()
    assert all(request.method == "GET" and request.url.host == "registry.test" for request in candidate_api.requests)
    validated = candidate_api.client.post(BASE + "/candidate-plans/validate", json={"candidate": request, "plan_digest": plan["plan_digest"]})
    assert validated.status_code == 200 and validated.json()["data"] == plan


def test_same_name_same_manifest_different_source_is_distinct(candidate_api: CandidateApi) -> None:
    plans = [candidate_api.client.post(BASE + "/candidate-plans", json=candidate_api.request(candidate_api.source(endpoint))).json()["data"]
             for endpoint in ("https://registry.test", "https://other.test")]
    assert plans[0]["manifest_digest"] == plans[1]["manifest_digest"]
    assert plans[0]["plan_digest"] != plans[1]["plan_digest"]


def test_multiple_same_type_candidates_never_default_to_first(candidate_api: CandidateApi) -> None:
    request = candidate_api.request(index=1)
    request.pop("variant")
    response = candidate_api.client.post(BASE + "/candidate-plans", json=request)
    assert response.status_code == 422
    assert len(candidate_api.requests) == 1


@pytest.mark.parametrize("change", ["remove", "reorder", "replace", "404"])
def test_consumption_rechecks_manifest_and_selected_variant(candidate_api: CandidateApi, change: str) -> None:
    request = candidate_api.request(index=1)
    original = candidate_api.client.post(BASE + "/candidate-plans", json=request).json()["data"]
    server = cast(dict[str, object], candidate_api.raw["server"])
    packages = cast(list[object], server["packages"])
    if change == "remove":
        packages.pop()
    elif change == "reorder":
        packages.reverse()
    elif change == "replace":
        packages[1] = {"registryType": "pypi", "identifier": "different", "version": "2.0", "transport": {"type": "stdio"}}
    else:
        candidate_api.status = 404
    response = candidate_api.client.post(BASE + "/candidate-plans/validate", json={"candidate": request, "plan_digest": original["plan_digest"]})
    assert response.status_code == 409, response.text


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_http_is_configuration_only_and_sse_is_explicitly_rejected(candidate_api: CandidateApi, transport: str) -> None:
    definition = {"remotes": [{"type": transport, "url": "https://service.test/mcp", "headers": [{"name": "X-Mode", "value": "private-mode"}]}]}
    source = {"kind": "manual", "name": "manual-http", "version": "1", "definition": definition}
    response = candidate_api.client.post(BASE + "/candidate-plans", json=candidate_api.request(source))
    if transport == "sse":
        assert response.status_code == 422 and "SSE" in response.text
    else:
        assert response.status_code == 200, response.text
        plan = response.json()["data"]
        assert plan["disposition"] == "configuration_only" and plan["download_required"] is False
        assert plan["parameters"]["transport"] == "streamable_http" and "private-mode" not in response.text
    assert not candidate_api.requests


def test_explicit_manual_command_keeps_unmanaged_contract(candidate_api: CandidateApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANDIDATE_TEST_COMMAND", "user-explicit-command")
    source = {"kind": "manual", "name": "manual-command", "version": "user-defined",
              "definition": {"transport": "stdio", "command": "${CANDIDATE_TEST_COMMAND}", "args": ["--dynamic"], "env": {"TOKEN": "private-env"}}}
    response = candidate_api.client.post(BASE + "/candidate-plans", json=candidate_api.request(source))
    assert response.status_code == 200, response.text
    plan = response.json()["data"]
    assert plan["disposition"] == "explicit_command" and plan["download_required"] is False
    assert plan["parameters"]["command"] == "user-explicit-command" and "private-env" not in response.text
    assert not candidate_api.requests


@pytest.mark.parametrize("mutation", ["backend", "gateway", "platform"])
def test_forged_target_and_platform_never_fetch_source(candidate_api: CandidateApi, mutation: str) -> None:
    request: dict[str, object] = {"target": deepcopy(TARGET), "source": candidate_api.source()}
    if mutation == "platform":
        request["platform"] = {"operating_system": "browser-claimed"}
    else:
        cast(dict[str, str], request["target"])[mutation + "_id"] = ("00000000-0000-4000-8000-000000000022" if mutation == "backend"
                                                                     else "gateway_00000000000040008000000000000002")
    response = candidate_api.client.post(BASE + "/candidate-manifests", json=request)
    assert response.status_code == (422 if mutation == "platform" else 409)
    assert not candidate_api.requests


def test_new_secret_input_is_blocked_without_secret_body_or_request_log(candidate_api: CandidateApi, caplog: pytest.LogCaptureFixture) -> None:
    source = {"kind": "manual", "name": "private-http", "version": "1", "definition": {"remotes": [{"type": "streamable-http",
              "url": "https://service.test/mcp", "headers": [{"name": "Authorization", "isSecret": True, "value": "private-input-sentinel"}]}]}}
    response = candidate_api.client.post(BASE + "/candidate-plans", json=candidate_api.request(source))
    assert response.status_code == 422 and "secret input" in response.text
    assert "private-input-sentinel" not in response.text + caplog.text


def test_explicit_v0_uses_only_that_protocol_and_freezes_entry_id(candidate_api: CandidateApi) -> None:
    candidate_api.raw = {"name": "org.example/same", "version": "release-1", "packages": [{"registry_type": "npm", "identifier": "@example/old",
                         "version": "1.2.3", "environment_variables": [{"name": "MODE", "value": "old"}]}],
                         "_meta": {"io.modelcontextprotocol.registry/official": {"id": "stable-entry"}}}
    source = candidate_api.source()
    source.update(protocol="v0", entry_id="stable-entry", entry_url="https://registry.test/v0/servers/stable-entry")
    response = candidate_api.client.post(BASE + "/candidate-plans", json=candidate_api.request(source))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["source"]["entry_id"] == "stable-entry"
    assert all(request.url.path == "/v0/servers/stable-entry" for request in candidate_api.requests)


def test_v01_envelope_does_not_invent_entry_id_from_publisher_metadata(candidate_api: CandidateApi) -> None:
    server = cast(dict[str, object], candidate_api.raw["server"])
    server["_meta"] = {"io.modelcontextprotocol.registry/publisher-provided": {"github": {"readme": "readme"}},
                       "io.modelcontextprotocol.registry/official": {"id": "publisher-cannot-assert-registry-id"}}
    candidate_api.raw["_meta"] = {"io.modelcontextprotocol.registry/official": {
        "status": "active", "isLatest": True, "publishedAt": "2026-11-05T00:00:00Z"}}
    request = candidate_api.request()
    response = candidate_api.client.post(BASE + "/candidate-plans", json=request)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["source"]["entry_id"] is None
    source = {**candidate_api.source(), "entry_id": "publisher-cannot-assert-registry-id"}
    rejected = candidate_api.client.post(BASE + "/candidate-manifests", json={"target": TARGET, "source": source})
    assert rejected.status_code == 409 and "身份" in rejected.text


@pytest.mark.parametrize("protocol", ["v0.1", "v0"])
def test_enveloped_registry_identity_uses_only_top_level_official_metadata(candidate_api: CandidateApi, protocol: str) -> None:
    server = cast(dict[str, object], candidate_api.raw["server"])
    server["_meta"] = {"io.modelcontextprotocol.registry/official": {"id": "publisher-id"}}
    candidate_api.raw["_meta"] = {"io.modelcontextprotocol.registry/official": {"id": "registry-id"}}
    source = candidate_api.source()
    source.update(protocol=protocol, entry_id="registry-id", entry_url=f"https://registry.test/{protocol}/servers/registry-id")
    request = candidate_api.request(source)
    response = candidate_api.client.post(BASE + "/candidate-plans", json=request)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["source"]["entry_id"] == "registry-id"
    candidate_api.raw["_meta"] = {"io.modelcontextprotocol.registry/official": {"id": "changed-registry-id"}}
    rejected = candidate_api.client.post(BASE + "/candidate-plans", json=request)
    assert rejected.status_code == 409 and "身份" in rejected.text
    assert all(request.url.path == f"/{protocol}/servers/registry-id" for request in candidate_api.requests)


@pytest.mark.parametrize("status", [302, 403, 503])
def test_registry_error_never_becomes_no_match_or_another_protocol(candidate_api: CandidateApi, status: int) -> None:
    candidate_api.status = status
    response = candidate_api.client.post(BASE + "/candidate-manifests", json={"target": TARGET, "source": candidate_api.source()})
    assert response.status_code == 503 and f"HTTP {status}" in response.text
    assert len(candidate_api.requests) == 1


def test_changed_native_environment_invalidates_original_plan(candidate_api: CandidateApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CANDIDATE_TEST_COMMAND", "first-command")
    source = {"kind": "manual", "name": "manual-command", "version": "1",
              "definition": {"transport": "stdio", "command": "${CANDIDATE_TEST_COMMAND}"}}
    request = candidate_api.request(source)
    plan = candidate_api.client.post(BASE + "/candidate-plans", json=request).json()["data"]
    monkeypatch.setenv("CANDIDATE_TEST_COMMAND", "second-command")
    response = candidate_api.client.post(BASE + "/candidate-plans/validate", json={"candidate": request, "plan_digest": plan["plan_digest"]})
    assert response.status_code == 409


@pytest.mark.parametrize("change", ["input", "platform"])
def test_changed_inputs_or_native_platform_invalidate_original_plan(candidate_api: CandidateApi, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    from app.services.infrastructure.mcp import catalog_owner

    source = {"kind": "manual", "name": "remote", "version": "1", "definition": {"remotes": [{
        "type": "streamable-http", "url": "https://service.test/{path}", "variables": {"path": {"isRequired": True}}}]}}
    request = {**candidate_api.request(source), "inputs": {"path": "first"}}
    response = candidate_api.client.post(BASE + "/candidate-plans", json=request)
    assert response.status_code == 200, response.text
    plan = response.json()["data"]
    if change == "input":
        request["inputs"] = {"path": "second"}
    else:
        monkeypatch.setattr(catalog_owner, "native_mcp_platform", lambda: McpTargetPlatformDTO(operating_system="different", architecture="different"))
    rejected = candidate_api.client.post(BASE + "/candidate-plans/validate", json={"candidate": request, "plan_digest": plan["plan_digest"]})
    assert rejected.status_code == 409 and not candidate_api.requests


@pytest.mark.parametrize("entry_url", ["https://other.test/v0.1/servers/same", "https://registry.test/v0/servers/same",
                                      "https://registry.test/v0.1/servers/%252e%252e/same", "https://user:secret@registry.test/v0.1/servers/same"])
def test_source_url_cannot_escape_selected_registry_or_protocol(candidate_api: CandidateApi, entry_url: str) -> None:
    source = {**candidate_api.source(), "entry_url": entry_url}
    response = candidate_api.client.post(BASE + "/candidate-manifests", json={"target": TARGET, "source": source})
    assert response.status_code == 422 and not candidate_api.requests
    assert "user:secret" not in response.text


@pytest.mark.parametrize("failure", ["invalid_json", "size"])
def test_source_reader_reports_invalid_or_unbounded_manifest(candidate_api: CandidateApi, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    if failure == "invalid_json":
        candidate_api.content = b"not-json"
    else:
        monkeypatch.setattr(candidate_source, "_MAX_MANIFEST_BYTES", 64)
        candidate_api.content = b" " * 65
    response = candidate_api.client.post(BASE + "/candidate-manifests", json={"target": TARGET, "source": candidate_api.source()})
    assert response.status_code == 503 and len(candidate_api.requests) == 1
    assert ("JSON" if failure == "invalid_json" else "上限") in response.text
