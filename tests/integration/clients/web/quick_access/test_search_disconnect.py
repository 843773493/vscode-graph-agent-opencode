"""真实 HTTP 截断与正常 EOF 共用生产 SSE 编码，覆盖浏览器和 Python SDK。"""

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event
from urllib.parse import unquote

import httpx
import pytest

from app.gateway.routes.search import _encode_search_sse
from app.schemas.search_federation import (
    FederatedSearchEventDTO,
    FederatedSearchRequest,
)
from app.services.infrastructure.gateway_search.client import (
    IncompleteSearchStreamError,
)
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.http_server import running_http_server
from tests.support.paths import chromium_temporary_environment

OWNER = {"gateway_id": "gateway_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "workspace_id": "019a0000-0000-7000-8000-000000000001"}


@pytest.fixture
def run_context(request: pytest.FixtureRequest) -> TestRunContext:
    return TestRunContext.from_test_file(Path(request.node.path)).prepare()


@pytest.fixture
def fault_server(run_context: TestRunContext) -> Iterator[tuple[str, list[dict[str, object]]]]:
    releases: dict[str, Event] = {}
    calls: list[dict[str, object]] = []
    errors: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *_args: object) -> None:
            pass

        def send_body(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.startswith("/release/"):
                releases[unquote(self.path.removeprefix("/release/"))].set()
                self.send_body(b"ok", "text/plain")
            elif self.path == "/fixture.mjs":
                self.send_body((run_context.artifacts_dir / "fixture.mjs").read_bytes(), "text/javascript")
            elif self.path == "/api/gateway/auth/local-credential":
                self.send_body(b'{"data":{"token":"isolated-test"}}', "application/json")
            elif self.path == "/":
                self.send_body(b"<!doctype html><title>HTTP search transport</title>", "text/html")
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            assert self.path in {"/api/gateway/search", "/api/gateway/search/agent"}
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            request_id, scoped, truncated = payload["queries"][0]["query"].split(":")
            event = Event()
            releases[request_id] = event
            body = b": heartbeat\n\n"
            if scoped == "scope":
                body += _encode_search_sse(FederatedSearchEventDTO.model_validate({
                    "sequence": 1, "request_id": request_id, "type": "scope", "scope": {
                        "scope": {"gateway_id": OWNER["gateway_id"], "resolved_at": datetime.now(UTC),
                                  "mode": "origin", "owners": [OWNER]},
                        "branches": [{"owner": OWNER, "state": "registered"}],
                    },
                }))
            calls.append({"path": self.path, "request_id": request_id, "query": payload["queries"][0]["query"],
                          "header_request_id": self.headers.get("X-Request-ID")})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Request-ID", request_id)
            self.send_header("Content-Length", str(len(body) + (1 if truncated == "truncated" else 0)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            # 浏览器在收到真实响应头后放行；scope 场景由真正消费事件的一方放行。
            if not event.wait(10):
                errors.append(f"未收到消费确认: {request_id}")
            self.close_connection = True

    with running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as server:
        yield f"http://127.0.0.1:{server.server_port}", calls
    assert errors == []


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("truncated", [False, True])
@pytest.mark.asyncio
async def test_search_disconnect_retains_request_and_original_network_cause(
    scoped: bool, truncated: bool, run_context: TestRunContext,
    fault_server: tuple[str, list[dict[str, object]]],
) -> None:
    url, calls = fault_server
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "真实 HTTP 验收需要 Chromium"
    suffix = f"{'scope' if scoped else 'headers'}:{'truncated' if truncated else 'eof'}"
    command = ["bun", "tests/integration/clients/web/quick_access/search_disconnect.mjs", url, suffix,
               str(run_context.artifacts_dir)]
    with chromium_temporary_environment(run_context.runtime_root_for_node(suffix) / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=45,
                                        env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium})
    (run_context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout)
    (run_context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr)
    (run_context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    browser = json.loads((run_context.artifacts_dir / "browser.json").read_text())
    assert browser["events"] == (["scope"] if scoped else [])
    assert browser["error"]["requestId"] == "browser"
    assert browser["error"]["code"] == ("stream_read_failed" if truncated else "incomplete_stream")
    assert browser["pageErrors"] == []
    if truncated:
        assert browser["error"]["causeName"] == "TypeError"
        assert browser["error"]["causeMessage"]
        assert browser["error"]["causeMessage"] in browser["error"]["message"]

    received = []
    client = GatewaySessionContextClient(gateway_url=url, local_token_supplier=lambda: "isolated-test")
    try:
        async with httpx.AsyncClient() as release:
            if not scoped:
                # HTTPX 的原 response hook 只观察真实响应，不替换 transport 或正文。
                async def headers_received(response: httpx.Response) -> None:
                    await release.get(url + "/release/python")

                client._http_client.event_hooks["response"].append(headers_received)
            with pytest.raises(httpx.RemoteProtocolError if truncated else IncompleteSearchStreamError) as caught:
                async for item in client.resource_search.search(FederatedSearchRequest.model_validate({
                    "scope": {"mode": "origin", "origin": OWNER},
                    "queries": [{"category": "catalog", "query": f"python:{suffix}", "limit": 1}],
                }), request_id="python"):
                    received.append(item.type)
                    await release.get(url + "/release/python")
        if isinstance(caught.value, httpx.RequestError):
            assert caught.value.request.headers["X-Request-ID"] == "python"
            assert "without sending complete message body" in str(caught.value)
        else:
            assert caught.value.request_id == "python"
        assert received == (["scope"] if scoped else [])
    finally:
        await client.aclose()
    assert len(calls) == 2
    assert [call["request_id"] for call in calls] == ["browser", "python"]
    assert calls[1]["header_request_id"] == "python"
    (run_context.artifacts_dir / "http-calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=2))
