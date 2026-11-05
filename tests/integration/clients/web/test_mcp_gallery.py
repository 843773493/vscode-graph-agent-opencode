"""真实 Gateway/Workbench 查询受控 MCP registry；不启动、安装或调用 MCP 服务。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414 - 正式产物只构建一次
)
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process


class RegistryRequest(TypedDict):
    method: str
    path: str
    query: dict[str, list[str]]
    client_cancelled: NotRequired[bool]


@pytest.fixture
def gallery_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    block = integration_port_block_for_file(Path(request.node.path))
    lease, probes = E2EProcessLease.acquire_available(
        context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json",
        (port for port in range(block.port(20), block.end + 1) if port != block.port(21)),
    )
    try:
        (context.artifacts_dir / "port-allocation.json").write_text(json.dumps({
            "block": {"start": block.start, "end": block.end}, "port": lease.ports[0], "probes": probes,
        }, indent=2) + "\n", encoding="utf-8")
        yield lease.ports[0]
    finally:
        lease.release()


@pytest.fixture
def mcp_registry(request: pytest.FixtureRequest) -> Iterator[tuple[str, list[RegistryRequest]]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    records: list[RegistryRequest] = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            record: RegistryRequest = {"method": "GET", "path": url.path, "query": query}
            records.append(record)
            supported = url.path in {"/alpha/v0.1/servers", "/beta/v0.1/servers", "/legacy/v0/servers"}
            status = 404 if not supported else 503 if query.get("search") == ["fail"] or query.get("cursor") == ["failed+/="] else 200
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if query.get("search") == ["slow"] and url.path.startswith("/alpha/"):
                assert release.wait(30), "浏览器未释放旧来源的慢请求"
            if status >= 400:
                data = {"error": "MCP registry HTTP failure"}
            elif query.get("search") == ["nomatch"]:
                data = {"servers": [], "metadata": {"count": 0}}
            elif url.path == "/legacy/v0/servers":
                data = {"servers": [{
                    "name": "legacy/server", "description": "Legacy directory entry", "version": "1.0.0",
                    "packages": [{"registry_type": "npm", "identifier": "legacy-server", "version": "1.0.0", "transport": {"type": "stdio"}}],
                    "_meta": {"io.modelcontextprotocol.registry/official": {"id": "legacy-id", "is_latest": True}},
                }], "metadata": {"count": 1}}
            else:
                source = "Beta" if url.path.startswith("/beta/") else "Alpha"
                second = "cursor" in query
                count = 3 if second else 50
                data = {"servers": [{"server": {
                    "name": f"test.{source.lower()}/server-{index + (50 if second else 0)}",
                    "title": f"Gallery {source} {index + (50 if second else 0)}",
                    "description": f"{source} selected registry entry · {query.get('search', ['browse'])[0]}", "version": "1.0.0",
                    "packages": [{"registryType": "npm", "identifier": f"gallery-{source.lower()}-{index}", "transport": {"type": "stdio"}}],
                    "_meta": {"io.modelcontextprotocol.registry/publisher-provided": {"github": {"readme": f"# Gallery {source}\nRead-only registry description."}}},
                }, "_meta": {}} for index in range(count)],
                    "metadata": {"count": count, **({"nextCursor": "failed+/=" if query.get("search") == ["badpage"] else "opaque+/="} if not second else {})}}
            try:
                self.wfile.write(json.dumps(data).encode())
            except (BrokenPipeError, ConnectionResetError):
                record["client_cancelled"] = True

        def do_POST(self) -> None:
            assert self.path == "/release", self.path
            release.set()
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

    port = integration_port_block_for_file(Path(request.node.path)).port(21)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", records
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "registry HTTP 线程未退出"
        (context.artifacts_dir / "registry-requests.json").write_text(json.dumps(records, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_gallery_selected_source_pages_errors_cancellation_and_read_only_details(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
    web_workbench_assets: Path,
    mcp_registry: tuple[str, list[RegistryRequest]],
    gallery_gateway_port: int,
) -> None:
    del integration_workspace_config_path
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert executable, "MCP Gallery 产品场景需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    registry_url, records = mcp_registry
    gateway = start_gateway_process(
        workspace_root=Path(integration_workspace_root_path), default_backend_url=None,
        port=gallery_gateway_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid),
        artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
    )
    try:
        command = ["bun", "tests/integration/clients/web/mcp_gallery.mjs"]
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            result = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root, timeout=150, check=False,
                capture_output=True, text=True, env={**os.environ, **temporary,
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": executable,
                    "BOXTEAM_GALLERY_FIXTURE": json.dumps({"baseUrl": f"http://127.0.0.1:{gateway.port}", "registryUrl": registry_url,
                        "artifacts": str(context.artifacts_dir)})})
        (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
        (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}), encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr
        observed = json.loads((context.artifacts_dir / "gallery-result.json").read_text())
        assert observed["unconfigured"] and observed["paginated"] and observed["noMatch"] and observed["httpFailure"]
        assert observed["paginationFailure"]
        assert observed["cancelledOnSourceChange"] and observed["readOnlyDetails"] and observed["explicitV0"]
        assert observed["v0RequiresExplicitVersion"]
        assert observed["pageErrors"] == []
        assert all(record["method"] == "GET" and record["query"].get("limit") != ["1"] for record in records)
        assert {record["path"] for record in records} == {
            "/alpha/v0.1/servers", "/beta/v0.1/servers", "/legacy/v0.1/servers", "/legacy/v0/servers",
        }
        assert any(record["query"].get("cursor") == ["opaque+/="] and "search" not in record["query"] for record in records)
    finally:
        close_gateway_process(gateway)
