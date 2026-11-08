"""Workspace 插件绑定的内部 owner 交付入口。"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_workspace_container
from app.api.runtime_control import (
    verify_local_control_request,
    verify_runtime_control_token,
)
from app.container import AppContainer
from app.core.trace_middleware import get_request_id, require_current_gateway_id
from app.schemas.gateway.service_plugin_workspace import WorkspacePluginSnapshot
from app.schemas.internal_v2.common import APIResponse

router = APIRouter(
    prefix="/api/internal/service-plugins",
    include_in_schema=False,
    dependencies=[Depends(verify_local_control_request), Depends(verify_runtime_control_token)],
)


@router.get("/bindings", response_model=APIResponse[WorkspacePluginSnapshot])
async def workspace_plugin_bindings(
    request: Request,
    request_id: Annotated[str, Depends(get_request_id)],
    container: Annotated[AppContainer, Depends(get_workspace_container)],
) -> APIResponse[WorkspacePluginSnapshot]:
    require_current_gateway_id("Workspace 插件绑定交付")
    backend_id = getattr(request.app.state, "backend_id", None)
    backend_generation = os.environ.get("BOXTEAM_BACKEND_GENERATION")
    if not isinstance(backend_id, str) or not backend_id or not backend_generation:
        raise HTTPException(status_code=503, detail="Backend 插件绑定 owner 身份尚未就绪")
    config_service = container.config_service
    with config_service.publication_lock:
        active = config_service.get_snapshot()
        bindings, referenced_bindings = config_service.get_workspace_plugin_binding_snapshot()
        snapshot = WorkspacePluginSnapshot(
            workspace_id=container.workspace_id,
            backend_id=backend_id,
            backend_generation=backend_generation,
            revision=active.revision,
            bindings=bindings,
            referenced_bindings=referenced_bindings,
        )
    return APIResponse(data=snapshot, request_id=request_id)
