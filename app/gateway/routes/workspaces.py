"""Gateway 工作区列表路由。"""

from __future__ import annotations

from functools import partial
from typing import Annotated

from boxteam_plugin_sdk.http import get_request_id
from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
)

from app.core.path_utils import get_gateway_root
from app.core.shielded_cleanup import run_sync_shielded
from app.gateway.auth import verify_gateway_token
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import (
    _wait_for_managed_runtime_restore_tasks,
    get_registry,
)
from app.schemas.gateway.management_targets import (
    GatewayManagementDirectoryDTO,
    GatewayManagementTargetDTO,
)
from app.schemas.gateway.workspaces import (
    GatewayClientRecoveryDTO,
    GatewayWorkspaceListDTO,
)
from app.schemas.internal_v2.common import APIResponse
from configs.maintenance.workspace_identity_commit import read_completed_client_recovery

router = APIRouter()


@router.get("/api/gateway/client-recovery", response_model=APIResponse[GatewayClientRecoveryDTO])
async def get_client_recovery(
    _: Annotated[str, Depends(verify_gateway_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
) -> APIResponse[GatewayClientRecoveryDTO]:
    recovery = await run_sync_shielded(partial(read_completed_client_recovery,
        gateway_root=get_gateway_root(), gateway_id=registry.gateway_id))
    return APIResponse(data=GatewayClientRecoveryDTO(gateway_id=registry.gateway_id, workspace_identity=recovery), request_id=request_id)


@router.get("/api/gateway/management/gateways", response_model=APIResponse[GatewayManagementDirectoryDTO])
async def list_management_gateways(
    _: Annotated[str, Depends(verify_gateway_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    registry: Annotated[GatewayWorkspaceRegistry, Depends(get_registry)],
) -> APIResponse[GatewayManagementDirectoryDTO]:
    items = [GatewayManagementTargetDTO(gateway_id=registry.gateway_id, name="本地", connection_kind="local")]
    items.extend(GatewayManagementTargetDTO(gateway_id=connection.remote_gateway_id, name=connection.name,
        connection_kind="remote_gateway", gateway_connection_id=connection.connection_id,
        connection_error=connection.connection_error, source_owner=connection.source_owner,
        remote_gateway_port=connection.remote_gateway_port) for connection in registry.remote_gateway_connections())
    return APIResponse(data=GatewayManagementDirectoryDTO(local_gateway_id=registry.gateway_id, items=items), request_id=request_id)


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
    _: str = Depends(verify_gateway_token),
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
            active_workspace=registry.active_workspace_dto,
            items=await registry.list_dtos(check_health=check_health),
        ),
        request_id=request_id,
    )
