"""精确插件业务代理的失败归属与零重放。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.main import gateway_http_exception_handler
from app.gateway.plugins.packages import PluginPackageStore
from app.gateway.runtime.service_plugins.runtime import GatewayPluginRuntime
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.gateway.server.service_plugin_proxy import router


@pytest.fixture
async def plugin_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[FastAPI, list[httpx.Request]]]:
    calls: list[httpx.Request] = []

    async def fail(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("具名插件连接已退出", request=request)

    skill_sources = GatewaySkillSourceOwner(gateway_id="gateway_" + "a" * 32, skills_root=tmp_path / "skills")
    await skill_sources.start()
    owner = GatewayPluginRuntime(home=tmp_path, gateway_id="gateway_" + "a" * 32,
        packages=PluginPackageStore(tmp_path / "packages"), settings=dict, workspace_root=lambda _: tmp_path,
        skill_sources=skill_sources)
    await owner.client.aclose()
    owner.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    monkeypatch.setattr(owner, "service", lambda *args, **kwargs: ("http://127.0.0.1:49152/services/echo", "private", asyncio.Event()))
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.add_exception_handler(HTTPException, gateway_http_exception_handler)
    app.include_router(router)
    app.state.service_plugins = owner
    from app.gateway.registry import GatewayWorkspaceRegistry
    app.state.registry = GatewayWorkspaceRegistry(storage_path=tmp_path / "registry.json", gateway_id=owner.gateway_id)
    app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="local")
    try:
        yield app, calls
    finally:
        await owner.close()
        await skill_sources.close()


async def test_service_connect_failure_is_attributed_without_replay(
    plugin_proxy: tuple[FastAPI, list[httpx.Request]], caplog: pytest.LogCaptureFixture,
) -> None:
    app, calls = plugin_proxy
    caplog.set_level(logging.ERROR)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/gateway/service-plugins/probe.tools/probe/instances/original/services/echo/write?package_version=1.0.0",
            json={"value": 1}, headers={"X-Request-ID": "plugin-original-request"})
    assert response.status_code == 502
    assert response.headers["X-Request-ID"] == response.json()["request_id"] == "plugin-original-request"
    assert "instance_id=original" in response.json()["detail"]
    assert len(calls) == 1
    assert calls[0].headers["X-Request-ID"] == "plugin-original-request"
    assert calls[0].headers["X-Local-Token"] == "private"
    assert "具名插件连接已退出" in caplog.text


async def test_service_path_cannot_escape_declared_service(plugin_proxy: tuple[FastAPI, list[httpx.Request]]) -> None:
    app, calls = plugin_proxy
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/gateway/service-plugins/probe.tools/probe/instances/original/services/echo/%252e%252e/internal?package_version=1.0.0")
    assert response.status_code == 422
    assert calls == []
