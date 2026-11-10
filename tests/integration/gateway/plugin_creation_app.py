"""创建默认插件的显式故障组合；关键状态仍由真实创建与配置 writer 产生。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse, Response

import app.gateway.lifespan as gateway_lifespan
import app.gateway.routes.workspaces_managed as managed_routes
from app.gateway.main import app
from app.gateway.plugins.workspaces import WorkspacePluginBindings
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.gateway.service_plugin_workspace import WorkspacePluginCreation

_gate = Path(os.environ["BOXTEAM_PLUGIN_CREATION_TEST_GATE"])
_audit = Path(os.environ["BOXTEAM_PLUGIN_CREATION_TEST_AUDIT"])
_cancel_identity = os.environ.get("BOXTEAM_PLUGIN_CREATION_TEST_MODE") == "cancelled"
_creation_task: ContextVar[tuple[asyncio.AbstractEventLoop, asyncio.Task[object]] | None] = ContextVar(
    "creation_task", default=None)


def _record_creation(target: WorkspaceTarget, creation: WorkspacePluginCreation) -> None:
    with _audit.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"workspace_id": target.workspace_id, "creation": creation.model_dump(mode="json")}) + "\n")


class ObservedWorkspaceBindings(WorkspacePluginBindings):
    async def _initialize_creation(self, target: WorkspaceTarget, creation: WorkspacePluginCreation) -> None:
        _record_creation(target, creation)
        await super()._initialize_creation(target, creation)


gateway_lifespan.WorkspacePluginBindings = ObservedWorkspaceBindings

if _cancel_identity:
    _create = managed_routes.create_direct_managed_workspace
    _identity = GatewayWorkspaceRegistry.resolve_local_workspace_identity

    def cancel_after_real_identity(self, project_root, workspace_root, *, create_directory=False):
        workspace_id = _identity(self, project_root, workspace_root, create_directory=create_directory)
        owner = _creation_task.get()
        if create_directory and owner is not None and _gate.exists():
            # 实际身份入口已经完成；取消原请求任务，登记与intent结清仍由产品实现。
            loop, task = owner
            loop.call_soon_threadsafe(task.cancel)
        return workspace_id

    async def interrupted_create(**kwargs):
        task = asyncio.current_task()
        assert task is not None
        token = _creation_task.set((asyncio.get_running_loop(), task))
        try:
            return await _create(**kwargs)
        except asyncio.CancelledError:
            target = next(item for item in kwargs["registry"].targets()
                if item.root_path == str(Path(kwargs["root_path"]).resolve()))
            creation = WorkspacePluginCreation.model_validate(target.plugin_creation)
            _record_creation(target, creation)
            raise HTTPException(503, "controlled creation request cancelled after identity") from None
        finally:
            _creation_task.reset(token)

    GatewayWorkspaceRegistry.resolve_local_workspace_identity = cancel_after_real_identity
    managed_routes.create_direct_managed_workspace = interrupted_create

@app.middleware("http")
async def interrupt_creation_validation(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    if (request.method == "POST" and request.url.path.startswith("/api/internal/service-plugins/workspaces/")
            and request.url.path.endswith("/validate") and _gate.exists()):
        candidate = await request.json()
        if any(binding["package_id"] == "probe.defaults" for binding in candidate["bindings"]):
            # 只拒绝真实 HTTP 验证，不替产品写入 intent、候选、配置或终态。
            return JSONResponse({"detail": "controlled creation validation unavailable"}, status_code=503,
                headers={"X-Request-ID": request.headers["X-Request-ID"]})
    return await call_next(request)
