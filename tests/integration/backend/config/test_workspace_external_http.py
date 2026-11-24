"""真实 Workspace 快照到网页、搜索和 Skill HTTP 的代理捕获。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest
from boxteam_plugin_sdk.lifecycle import LifetimeScope

from app.agents.tools.web import create_fetch_webpage_tool, create_web_search_tool
from app.core.workspace_identity import read_workspace_id
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.gateway_session_context_client import (
    GatewaySessionContextClient,
)
from app.services.infrastructure.resource_platform.bootstrap import (
    bootstrap_resource_platform,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from configs.installer import install_user_configuration
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace


@dataclass
class Capture:
    url: str = ""
    requests: list[str] = field(default_factory=list)

    async def respond(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            length = next((int(line.partition(":")[2]) for line in lines if line.lower().startswith("content-length:")), 0)
            if length:
                await reader.readexactly(length)
            self.requests.append(lines[0])
            path = urlsplit(lines[0].split()[1]).path
            if path == "/search":
                content_type = "text/html"
                body = b'<div class="body"><h2>Python</h2><a href="https://python.org/">Python documentation</a></div>'
            elif path == "/news":
                content_type = "application/json"
                body = json.dumps({"results": [{"date": "2026-11-24", "title": "Python news", "excerpt": "Python release",
                    "url": "https://python.org/news/", "source": "Python"}]}).encode()
            else:
                content_type, body = "text/plain", b"workspace web content"
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.fixture
async def endpoints() -> AsyncIterator[tuple[Capture, Capture]]:
    upstream, proxy = Capture(), Capture()
    servers = []
    for capture in (upstream, proxy):
        server = await asyncio.start_server(capture.respond, "127.0.0.1", 0)
        capture.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        servers.append(server)
    try:
        yield upstream, proxy
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


@dataclass
class WorkspaceHTTP:
    workspace: Path
    service: ConfigService

    async def configure(self, proxy: dict[str, object], *, source: dict[str, object] | None = None) -> None:
        settings: dict[str, object] = {"network": {"proxy": proxy}}
        if source is not None:
            settings["workspace"] = {"resources": {"skill_sources": {"sources": [source]}}}
        (self.workspace / ".boxteam/workspace.jsonc").write_text(json.dumps({
            "config_version": 4, "extends": "user:workspace_local", "settings": settings,
        }), encoding="utf-8")
        await self.service.reload()
        assert self.service.get_snapshot().graph.resolve_scope().settings["network"]["proxy"]["mode"] == proxy["mode"]


@pytest.fixture
async def workspace_http(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkspaceHTTP]:
    # 本组只验内置消费者；原 SDK 的可选元数据下载另有独立出口验收。
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = prepare_default_test_workspace(workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace")
    config_root = context.boxteam_home / "config"
    install_user_configuration(config_root=config_root, profile="default", workspace_root=workspace)
    service = ConfigService(config_dir=config_root, config_path=config_root / "workspace.jsonc", workspace_root=workspace)
    service.validate_workspace_config()
    try:
        yield WorkspaceHTTP(workspace, service)
    finally:
        await service.close()


@pytest.fixture
def search_engines(monkeypatch: pytest.MonkeyPatch, endpoints: tuple[Capture, Capture]) -> None:
    from ddgs.engines import ENGINES
    from ddgs.engines.duckduckgo import Duckduckgo
    from ddgs.engines.duckduckgo_news import DuckduckgoNews

    upstream, _ = endpoints

    class LocalText(Duckduckgo):
        search_url = upstream.url + "/search"

    class LocalNews(DuckduckgoNews):
        search_url = upstream.url + "/news"

        def _get_vqd(self, query: str) -> str:
            return self.http_client.request("GET", upstream.url + "/token", params={"q": query}).text

    monkeypatch.setitem(ENGINES, "text", {"duckduckgo": LocalText})
    monkeypatch.setitem(ENGINES, "news", {"duckduckgo": LocalNews})


@pytest.mark.parametrize("mode", ["environment", "explicit", "direct", "bypass"])
async def test_web_tools_and_skill_fetch_use_workspace_routes(
    workspace_http: WorkspaceHTTP, endpoints: tuple[Capture, Capture], search_engines: None,
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    case = workspace_http
    upstream, proxy = endpoints
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    configured: dict[str, object] = {"mode": mode}
    if mode in {"explicit", "bypass"}:
        configured = {"mode": "explicit", "url": proxy.url, "bypass": ["127.0.0.1"] if mode == "bypass" else []}
    await case.configure(configured)
    # 普通调用只能使用快照捕获的环境，不能借 SDK 隐式读取这次变更。
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("DDGS_PROXY", "http://127.0.0.1:1")
    context = MagicMock(workspace_root=case.workspace, config_service=case.service)
    output = MagicMock()
    output.materialize.return_value = SimpleNamespace(read_path="tool-result", line_count=1, byte_count=21)
    monkeypatch.setattr("app.agents.tools.web.ToolOutputStore", lambda **_kwargs: output)
    fetched = json.loads(await create_fetch_webpage_tool(context).ainvoke({"urls": [upstream.url + "/page"]}))
    assert fetched["pages"][0]["status_code"] == 200
    for search_type in ("text", "news"):
        result = json.loads(await create_web_search_tool(context).ainvoke({"query": "Python", "search_type": search_type}))
        assert result["results"][0]["url"].startswith("https://python.org/")
        assert result["returned_result_count"] == 1
    workspace_id = read_workspace_id(case.workspace)
    platform = bootstrap_resource_platform(workspace_root=case.workspace, workspace_id=workspace_id,
        file_paths=WorkspaceFilePaths(workspace=case.workspace, workspace_id=workspace_id, bindings=None),
        workspace_proxy_routes=case.service.resolve_workspace_proxy_routes)
    scope = LifetimeScope("http-integration")
    try:
        activate = await platform.prepare_config({"resources": {"skill_sources": {"gateway_updates": {"mode": "event"},
            "sources": [{"id": "remote", "name": "remote", "fetch": {"protocol": "http", "url": upstream.url + "/skill"},
                         "updates": {"mode": "manual"}}]}}}, scope)
        activate()
        assert platform.configured_skill_sources.entries()[0].content == "workspace web content"
    finally:
        await scope.close()
        await platform.close()
    selected, unused = (proxy, upstream) if mode in {"explicit", "environment"} else (upstream, proxy)
    assert len(selected.requests) == 5
    assert unused.requests == []
    assert all(("http://" in line.split()[1]) == (selected is proxy) for line in selected.requests)


async def test_skill_candidate_and_refresh_follow_their_actual_snapshots(
    workspace_http: WorkspaceHTTP, endpoints: tuple[Capture, Capture],
) -> None:
    case = workspace_http
    upstream, proxy = endpoints
    workspace_id = read_workspace_id(case.workspace)
    platform = bootstrap_resource_platform(workspace_root=case.workspace, workspace_id=workspace_id,
        file_paths=WorkspaceFilePaths(workspace=case.workspace, workspace_id=workspace_id, bindings=None),
        workspace_proxy_routes=case.service.resolve_workspace_proxy_routes)
    source = {"id": "remote", "name": "remote", "fetch": {"protocol": "http", "url": upstream.url + "/skill"},
              "updates": {"mode": "manual"}}
    try:
        await case.service.start_watching(shadow_reconcile=platform.prepare_config)
        await case.configure({"mode": "direct"})
        await case.configure({"mode": "explicit", "url": proxy.url}, source=source)
        # 首次获取发生在候选发布前；旧 active 是 direct，借旧快照会到错误的上游。
        assert proxy.requests == [f"GET {upstream.url}/skill HTTP/1.1"]
        assert upstream.requests == []
        await case.configure({"mode": "direct"}, source=source)
        await platform.configured_skill_sources.refresh()
        assert upstream.requests == ["GET /skill HTTP/1.1"]
        assert len(proxy.requests) == 1
    finally:
        await case.service.stop_watching()
        await platform.close()


async def test_internal_gateway_context_ignores_external_proxy(
    workspace_http: WorkspaceHTTP, endpoints: tuple[Capture, Capture], monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream, proxy = endpoints
    monkeypatch.setenv("ALL_PROXY", proxy.url)
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setenv("http_proxy", proxy.url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    client = GatewaySessionContextClient(gateway_url=upstream.url, local_token_supplier=lambda: "test-token")
    try:
        response = await client._http_client.get(upstream.url + "/internal", timeout=2)
        assert response.text == "workspace web content"
    finally:
        await client.aclose()
    assert upstream.requests == ["GET /internal HTTP/1.1"]
    assert proxy.requests == []
