"""服务插件卸载竞态的专用 Gateway lifespan 组合。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from threading import Event

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from app.gateway.lifespan import lifespan as gateway_lifespan
from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)


class _RaceGate:
    def __init__(self) -> None:
        self.armed = asyncio.Event()
        self.references_read = asyncio.Event()
        self.validate_arrived = asyncio.Event()
        self.release_uninstall = asyncio.Event()
        self.read_uri: str | None = None
        self.read_entered = Event()
        self.release_read = Event()


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[dict[str, Callable[[], Awaitable[None]] | None]]:
    async with gateway_lifespan(application) as state:
        gate = _RaceGate()
        application.state.plugin_uninstall_race_gate = gate
        coordinator = application.state.workspace_plugin_bindings
        read_references = coordinator.read_references

        async def paused_read_references():
            references = await read_references()
            if gate.armed.is_set():
                gate.references_read.set()
                await gate.release_uninstall.wait()
            return references

        coordinator.read_references = paused_read_references
        navigate = ResourceNavigation.navigate_sync

        def paused_navigation(self: ResourceNavigation, request: ResourceNavigationRequest, **kwargs):
            if request.uri == gate.read_uri and request.operation == "read":
                gate.read_entered.set()
                if not gate.release_read.wait(30):
                    raise TimeoutError("测试未释放插件 Skill 实际读取")
            return navigate(self, request, **kwargs)

        ResourceNavigation.navigate_sync = paused_navigation
        try:
            yield state
        finally:
            gate.release_read.set()
            ResourceNavigation.navigate_sync = navigate


def create_gateway_app() -> FastAPI:
    """复用正式 Gateway，仅在本组合里暴露竞态同步点。"""
    from app.gateway.main import app

    app.router.lifespan_context = _lifespan

    @app.middleware("http")
    async def observe_validation_arrival(request: Request, call_next):
        if (request.method == "POST"
            and request.url.path.startswith("/api/internal/service-plugins/workspaces/")
            and request.url.path.endswith("/validate")):
            gate: _RaceGate = request.app.state.plugin_uninstall_race_gate
            if gate.armed.is_set():
                gate.validate_arrived.set()
        return await call_next(request)

    async def control(request: Request, action: str) -> JSONResponse:
        gate: _RaceGate = request.app.state.plugin_uninstall_race_gate
        if action == "arm":
            gate.armed.set()
        elif action == "wait-references":
            await asyncio.wait_for(gate.references_read.wait(), timeout=120)
        elif action == "wait-validate":
            await asyncio.wait_for(gate.validate_arrived.wait(), timeout=120)
        elif action == "release":
            gate.release_uninstall.set()
        elif action == "arm-read":
            gate.read_uri = (await request.json())["uri"]
        elif action == "wait-read":
            if not await asyncio.to_thread(gate.read_entered.wait, 15):
                raise TimeoutError("插件 Skill 读取未进入真实 I/O 边界")
        elif action == "release-read":
            gate.release_read.set()
        else:
            return JSONResponse({"detail": "unknown test action"}, status_code=404)
        return JSONResponse({"ok": True})

    app.add_api_route("/__test/service-plugin-race/{action}", control, methods=["POST"], include_in_schema=False)
    return app
