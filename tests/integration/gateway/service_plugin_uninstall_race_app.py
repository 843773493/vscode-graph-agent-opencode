"""服务插件卸载竞态的专用 Gateway lifespan 组合。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from app.gateway.lifespan import lifespan as gateway_lifespan


class _RaceGate:
    def __init__(self) -> None:
        self.armed = asyncio.Event()
        self.references_read = asyncio.Event()
        self.validate_arrived = asyncio.Event()
        self.release_uninstall = asyncio.Event()


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[dict[str, object]]:
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
        yield state


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
        else:
            return JSONResponse({"detail": "unknown test action"}, status_code=404)
        return JSONResponse({"ok": True})

    app.add_api_route("/__test/service-plugin-race/{action}", control, methods=["POST"], include_in_schema=False)
    return app
