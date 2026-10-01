"""Gateway 工作区列表路由。"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
)

from app.core.trace_middleware import get_request_id
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import (
    _wait_for_managed_runtime_restore_tasks,
    get_registry,
)
from app.schemas.gateway import GatewayWorkspaceListDTO
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


async def _wait_for_managed_runtime_restores(request: Request) -> None:
    """让工作区列表在启动恢复完成后反映真实的运行时状态。"""
    restore_tasks = getattr(
        getattr(request.app, "state", None),
        "managed_runtime_restore_tasks",
        {},
    )
    await _wait_for_managed_runtime_restore_tasks(restore_tasks)


@router.get("/api/gateway/workspaces", response_model=APIResponse[GatewayWorkspaceListDTO])
async def list_workspaces(
    request: Request,
    check_health: bool = Query(
        default=True,
        description="是否探测所有工作区及其附属服务的健康状态",
    ),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    await _wait_for_managed_runtime_restores(request)
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(check_health=check_health),
        ),
        request_id=request_id,
    )
