"""MCP 协议测试的正式插件桥与真实 Gateway HTTP 路由。"""

from collections.abc import AsyncIterator
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.gateway.server.mcp_upstream_proxy import router
from tests.support.mcp.bridge_endpoint import (
    bridge_endpoint_factory as bridge_endpoint_factory,  # noqa: PLC0414 - 同目录测试共用
)
from tests.support.mcp.http_endpoint import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 同目录测试共用真实 HTTP MCP 服务
)
from tests.support.mcp.http_endpoint import serve_http_app


@pytest.fixture
async def mcp_gateway_config() -> AsyncIterator[Mock]:
    """OAuth／MCP 的两跳都走真实 HTTP，避免 ASGITransport 缓冲无限 SSE。"""
    gateway = FastAPI()
    gateway.add_middleware(TraceMiddleware)
    gateway.include_router(router)
    async with httpx.AsyncClient(trust_env=False, timeout=None) as upstream:
        gateway.state.http_client = upstream
        async with serve_http_app(gateway) as url:
            config = Mock()
            config.get_gateway_connection_url.return_value = url
            yield config
