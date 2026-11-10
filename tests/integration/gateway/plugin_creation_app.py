"""创建默认插件的显式故障组合；关键状态仍由真实创建与配置 writer 产生。"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import Request
from starlette.responses import JSONResponse, Response

import app.gateway.lifespan as gateway_lifespan
from app.gateway.main import app
from app.gateway.plugins.workspaces import WorkspacePluginBindings
from app.gateway.registry import WorkspaceTarget
from app.schemas.gateway.service_plugin_workspace import WorkspacePluginCreation

_gate = Path(os.environ["BOXTEAM_PLUGIN_CREATION_TEST_GATE"])
_audit = Path(os.environ["BOXTEAM_PLUGIN_CREATION_TEST_AUDIT"])


def _record_creation(target: WorkspaceTarget, creation: WorkspacePluginCreation) -> None:
    with _audit.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"workspace_id": target.workspace_id, "creation": creation.model_dump(mode="json")}) + "\n")


class ObservedWorkspaceBindings(WorkspacePluginBindings):
    async def _initialize_creation(self, target: WorkspaceTarget, creation: WorkspacePluginCreation) -> None:
        _record_creation(target, creation)
        await super()._initialize_creation(target, creation)


gateway_lifespan.WorkspacePluginBindings = ObservedWorkspaceBindings

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
