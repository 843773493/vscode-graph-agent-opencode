"""公共 HTTP 夹具的真实请求、失败回收与协议调用方接线。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from tests.integration.clients.web.itemized_context.test_provider_http_403 import (
    PROVIDER_API_KEY,
    PROVIDER_ERROR_MARKER,
)
from tests.integration.clients.web.itemized_context.test_provider_http_403 import (
    provider_403_ports as provider_403_ports,  # noqa: PLC0414 - 注入原场景端口装具。
)
from tests.integration.clients.web.itemized_context.test_provider_http_403 import (
    provider_403_server as provider_403_server,  # noqa: PLC0414 - 验证原场景HTTP装具。
)
from tests.integration.clients.web.test_mcp_gallery import (
    mcp_registry as mcp_registry,  # noqa: PLC0414 - 不启动浏览器或Gateway。
)
from tests.integration.gateway import test_mcp_artifact_installation as artifact_case
from tests.integration.stubs.http_stubs import generation_target_stub
from tests.support.http_server import running_http_server
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import ensure_local_port_available


@pytest.fixture(params=[HTTPServer, ThreadingHTTPServer], ids=["http", "threaded-http"])
def bound_server(request: pytest.FixtureRequest) -> Iterator[HTTPServer]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = "真实正文 Ω".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = request.param(("127.0.0.1", 0), Handler)
    try:
        yield server
    finally:
        server.server_close()


@pytest.mark.parametrize("body_fails", [False, True])
def test_http_context_serves_requests_and_releases_port(bound_server: HTTPServer, body_fails: bool) -> None:
    error = LookupError("场景准备失败")
    with pytest.raises(LookupError) if body_fails else nullcontext() as failure:
        with running_http_server(bound_server) as server, httpx.Client(trust_env=False) as client:
            response = client.get(f"http://127.0.0.1:{server.server_port}/", timeout=2)
            assert response.status_code == 200 and response.content.decode() == "真实正文 Ω"
            if body_fails:
                raise error
    if body_fails:
        assert failure.value is error
    assert bound_server.socket.fileno() == -1
    ensure_local_port_available(bound_server.server_port, service_name="HTTP 夹具回收验收")


def test_generation_stub_preserves_workspace_identity(request: pytest.FixtureRequest) -> None:
    port = integration_port_block_for_file(Path(request.node.path)).port(20)
    with generation_target_stub(port, workspace_id="fixture-workspace") as state, httpx.Client(trust_env=False) as client:
        response = client.get(f"http://127.0.0.1:{port}/api/v1/health", headers={
            "X-BoxTeam-Workspace-Id": "fixture-workspace",
        }, timeout=2)
        assert response.status_code == 200
        assert response.json()["workspace_id"] == "fixture-workspace"
        assert len(state.requests) == 1
    ensure_local_port_available(port, service_name="generation HTTP 夹具回收验收")


def test_gallery_http_fixture_preserves_registry_response(mcp_registry) -> None:
    url, records = mcp_registry
    with httpx.Client(trust_env=False) as client:
        response = client.get(url + "/alpha/v0.1/servers?search=nomatch", timeout=2)
    assert response.status_code == 200
    assert response.json() == {"servers": [], "metadata": {"count": 0}}
    assert records == [{"method": "GET", "path": "/alpha/v0.1/servers", "query": {"search": ["nomatch"]}}]


def test_provider_denial_fixture_preserves_status_and_request(provider_403_server, provider_403_ports) -> None:
    _, state = provider_403_server
    body = {"model": "fixture-model", "stream": True, "messages": []}
    with httpx.Client(trust_env=False) as client:
        response = client.post(f"http://127.0.0.1:{provider_403_ports[1]}/v1/chat/completions", json=body,
                               headers={"Authorization": f"Bearer {PROVIDER_API_KEY}"}, timeout=2)
    assert response.status_code == 403
    assert response.json()["error"]["message"] == PROVIDER_ERROR_MARKER
    assert state.requests == [{"path": "/v1/chat/completions", "authorization": f"Bearer {PROVIDER_API_KEY}",
                               "body": body, "response_status": 403}]


def test_artifact_index_setup_failure_closes_its_started_http_server(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = OSError("manifest 写入失败")
    servers: list[ThreadingHTTPServer] = []
    write_text = Path.write_text

    def fail_manifest(path: Path, *args, **kwargs):
        if path.name == "release-1":
            raise error
        return write_text(path, *args, **kwargs)

    def bind(*args, **kwargs):
        server = ThreadingHTTPServer(*args, **kwargs)
        servers.append(server)
        return server

    monkeypatch.setattr(Path, "write_text", fail_manifest)
    monkeypatch.setattr(artifact_case, "ThreadingHTTPServer", bind)
    fixture = artifact_case.artifact_index._get_wrapped_function()(request)
    try:
        with pytest.raises(OSError) as failure:
            next(fixture)
        assert failure.value is error
        server, = servers
        assert server.socket.fileno() == -1
        ensure_local_port_available(server.server_port, service_name="manifest 失败后 HTTP 回收验收")
    finally:
        fixture.close()
        for server in servers:
            server.shutdown()
            server.server_close()
