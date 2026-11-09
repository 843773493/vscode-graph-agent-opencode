"""显式 HTTP 协议探针验证 Gateway／Browser 客户端 TLS 配置和 TCP 取消。

探针仅替代远端 HTTP 边界，不证明 Gateway 的路由或插件生命周期。
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from app.runtime.http_tls import auxiliary_service_tls_context
from app.schemas.gateway.service_plugin import PluginBinding
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.terminal_manager_client import TerminalManagerClient
from tests.harness.python.run_context import TestRunContext
from tests.support.resource_owners.sessions import WORKSPACE_ID


@dataclass
class HttpProbe:
    url: str = ""
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    requests: list[dict[str, str]] = field(default_factory=list)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            first, *headers = head.split("\r\n")
            fields = dict(line.lower().split(": ", 1) for line in headers if line)
            self.requests.append({"request": first, **fields})
            if urlsplit(first.split(" ")[1]).path.endswith("/wait"):
                self.entered.set()
                assert await reader.read() == b""
                self.disconnected.set()
                return
            body = b'{"data":[]}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Connection: close\r\nContent-Length: 11\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.fixture
async def native_http() -> AsyncIterator[HttpProbe]:
    probe = HttpProbe()
    server = await asyncio.start_server(probe.handle, "127.0.0.1", 0)
    probe.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    async with server:
        yield probe


@pytest.fixture
def tls_loads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[ssl.SSLContext]]:
    auxiliary_service_tls_context.cache_clear()
    create: Callable[..., ssl.SSLContext] = ssl.create_default_context
    contexts: list[ssl.SSLContext] = []

    def record(*args, **kwargs) -> ssl.SSLContext:
        context = create(*args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(ssl, "create_default_context", record)
    # 两个客户端既有 trust_env=False 合同继续生效。
    monkeypatch.setenv("SSL_CERT_FILE", "/unused-native-service-ca.pem")
    yield contexts
    auxiliary_service_tls_context.cache_clear()


async def test_shared_tls_context_preserves_owner_headers_and_tcp_cancellation(
    native_http: HttpProbe, tls_loads: list[ssl.SSLContext], request: pytest.FixtureRequest,
) -> None:
    clients = (
        TerminalManagerClient(workspace_id=WORKSPACE_ID, gateway_url=native_http.url,
            binding=PluginBinding(package_id="boxteam.developer-tools", package_version="0.1.0",
                plugin_id="terminal", enabled=True, workspace_id=WORKSPACE_ID)),
        BrowserManagerClient(workspace_id=WORKSPACE_ID, config_service=Mock(), gateway_url=native_http.url,
            binding=PluginBinding(package_id="boxteam.developer-tools", package_version="0.1.0",
                plugin_id="browser", enabled=True, workspace_id=WORKSPACE_ID)),
    )
    try:
        started = perf_counter()
        for _ in range(3):
            assert await clients[0].list_resources() == []
            assert await clients[1].list_resources() == []
        elapsed = perf_counter() - started
        assert len(tls_loads) == 1
        assert tls_loads[0].verify_mode == ssl.CERT_REQUIRED
        assert tls_loads[0].check_hostname is True
        for client in clients:
            native_http.entered.clear()
            native_http.disconnected.clear()
            pending = asyncio.create_task(client._json_request("GET", "/wait"))
            try:
                await asyncio.wait_for(native_http.entered.wait(), 3)
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            with pytest.raises(asyncio.CancelledError):
                await pending
            await asyncio.wait_for(native_http.disconnected.wait(), 3)
            assert await client.list_resources() == []
        assert len(tls_loads) == 1
        assert len(native_http.requests) == 10
        for fields in native_http.requests:
            assert fields["x-boxteam-workspace-id"] == WORKSPACE_ID
            assert fields["x-boxteam-resource-owner"] in {"workspace-terminal", "workspace-browser"}
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))
    terminal_requests = [item for item in native_http.requests
                         if item["x-boxteam-resource-owner"] == "workspace-terminal"]
    for item in terminal_requests:
        target = urlsplit(item["request"].split(" ")[1])
        assert target.path.startswith(
            "/api/gateway/service-plugins/boxteam.developer-tools/terminal/services/http/"
        )
        assert target.query == f"package_version=0.1.0&workspace_id={WORKSPACE_ID}"
        assert item["x-local-token"]
    browser_requests = [item for item in native_http.requests
                        if item["x-boxteam-resource-owner"] == "workspace-browser"]
    for item in browser_requests:
        target = urlsplit(item["request"].split(" ")[1])
        assert target.path.startswith(
            "/api/gateway/service-plugins/boxteam.developer-tools/browser/services/http/"
        )
        assert target.query == f"package_version=0.1.0&workspace_id={WORKSPACE_ID}"
        assert item["x-local-token"]
    run = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    (run.artifacts_dir / "transport.json").write_text(json.dumps({
        "requests": native_http.requests, "certificate_loads": len(tls_loads),
        "six_request_seconds": elapsed, "scope": "显式协议探针的真实HTTP/取消；默认TLS校验配置，不代替Gateway代理或HTTPS握手验收",
    }, ensure_ascii=False, indent=2) + "\n")
