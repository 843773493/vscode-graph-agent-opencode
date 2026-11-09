from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from mcp import types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared._httpx_utils import create_mcp_http_client
from sse_starlette.sse import AppStatus
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.routing import Mount
from starlette.types import ASGIApp

from app.services.infrastructure.mcp.config import McpServerConfig
from app.services.infrastructure.mcp.invocation import CancellableMcpClientSession
from app.services.infrastructure.mcp.session_runtime import (
    McpServerSessionPort,
    _McpServerSession,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.mcp.schema_server import build_server


@dataclass(frozen=True, slots=True)
class HttpSchemaEndpoint:
    url: str
    context: TestRunContext
    definitions: Path
    receipts: Path


@asynccontextmanager
async def sdk_http_session_factory(
    server: McpServerConfig, on_change: Callable[[], None],
) -> AsyncIterator[McpServerSessionPort]:
    """协议集成测试直连真实 SDK；Gateway 路由由产品入口测试单独验证。"""
    connection = server.connection
    assert connection is not None and connection["transport"] == "streamable_http"

    async def changed(message: object) -> None:
        if isinstance(message, types.ServerNotification) and isinstance(message.root, types.ToolListChangedNotification):
            on_change()

    async with (
        create_mcp_http_client(headers=connection.get("headers")) as client,
        streamable_http_client(connection["url"], http_client=client) as (read, write, _),
        CancellableMcpClientSession(read, write, message_handler=changed) as session,
    ):
        await session.initialize()
        yield _McpServerSession(session)


@asynccontextmanager
async def serve_http_app(app: ASGIApp) -> AsyncIterator[str]:
    """持有真实监听 socket，统一收束 MCP 测试 HTTP 服务。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=1))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("HTTP Server 在就绪前退出")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


@pytest.fixture
def http_schema_endpoint_factory(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    # 同一 pytest 进程连续启动独立 Server 时，不能继承上一实例的 SSE 退出标志。
    monkeypatch.setattr(AppStatus, "should_exit", False)

    @asynccontextmanager
    async def endpoint(
        label: str, *, required_headers: dict[str, str] | None = None,
        request_observer: Callable[[Headers], None] | None = None,
        call_observer: Callable[[str, dict[str, object], Headers], None] | None = None,
        list_tools_observer: Callable[[], None] | None = None,
        call_result_provider: Callable[
            [str, dict[str, object]], list[types.TextContent] | types.CallToolResult | None
        ] | None = None,
        tool_definitions: dict[str, dict[str, object]] | None = None,
    ) -> AsyncIterator[HttpSchemaEndpoint]:
        context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
        definitions = context.artifacts_dir / f"{label}-definitions.json"
        definitions.write_text(json.dumps(tool_definitions if tool_definitions is not None else {
            "probe": {"type": "object"},
        }), encoding="utf-8")
        receipts = context.artifacts_dir / f"{label}-receipts.jsonl"
        manager = StreamableHTTPSessionManager(app=build_server(
            definitions, receipts, call_observer=call_observer,
            list_tools_observer=list_tools_observer, call_result_provider=call_result_provider,
        ))

        @asynccontextmanager
        async def lifespan(_app):
            async with manager.run():
                yield

        async def handle_request(scope, receive, send):
            if scope["type"] == "http":
                headers = Headers(scope=scope)
                if request_observer is not None:
                    request_observer(headers)
                if required_headers is not None and any(headers.get(key) != value for key, value in required_headers.items()):
                    await Response(status_code=401)(scope, receive, send)
                    return
            await manager.handle_request(scope, receive, send)

        app = Starlette(routes=[Mount("/mcp", app=handle_request)], lifespan=lifespan)
        async with serve_http_app(app) as url:
            yield HttpSchemaEndpoint(url=f"{url}/mcp/", context=context,
                                     definitions=definitions, receipts=receipts)
    return endpoint
