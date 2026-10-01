"""Gateway 本地目录浏览与工作区生命周期路由。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)

from app.core.env import get_project_root
from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.federation import request_remote_gateway_management
from app.gateway.managed_workspaces import create_direct_managed_workspace
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceTarget,
)
from app.gateway.remote_gateway import register_remote_gateway
from app.gateway.routes._shared import (
    _directory_listing,
    _gateway_root,
    get_registry,
)
from app.gateway.routes.workspaces_managed import (
    _remote_gateway_credential,
    _remote_http_error_detail,
)
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.runtime.process import wait_for_http_ok
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.server.port_forwarding import get_port_forward_manager
from app.gateway.ssh_connections import (
    list_ssh_connection_options,
    resolve_ssh_connection_request,
)
from app.gateway.workspace_ids import build_workspace_id
from app.schemas.gateway import (
    ActivateGatewayWorkspaceResultDTO,
    AddLocalWorkspaceRequest,
    AddRemoteGatewayRequest,
    GatewayDirectoryListDTO,
    GatewayRuntimeRestartResultDTO,
    GatewayRuntimeStateResultDTO,
    GatewayWorkspaceListDTO,
    ReorderGatewayWorkspacesRequest,
    SshConnectionOptionListDTO,
    UpdateGatewayWorkspaceRequest,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


def _workspace_name(root_path: str, fallback: str = "workspace") -> str:
    name = Path(root_path).name
    return name or fallback


def get_workspace_runtime_controller(
    request: Request,
) -> GatewayWorkspaceRuntimeController:
    controller = getattr(request.app.state, "workspace_runtime_controller", None)
    if not isinstance(controller, GatewayWorkspaceRuntimeController):
        raise RuntimeError("Gateway 工作区运行时控制器尚未初始化")
    return controller


@router.get(
    "/api/gateway/local-directories",
    response_model=APIResponse[GatewayDirectoryListDTO],
)
async def list_local_directories(
    path: str | None = Query(
        default=None,
        description="要浏览的本机目录；为空时使用用户主目录",
    ),
    limit: int = Query(default=120, ge=1, le=500),
    gateway_connection_id: str | None = Query(default=None),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    if gateway_connection_id is not None:
        query = urlencode(
            {
                **({"path": path} if path is not None else {}),
                "limit": limit,
            }
        )
        try:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(gateway_connection_id),
                credential=_remote_gateway_credential(gateway_connection_id),
                method="GET",
                path=f"/api/gateway/federation/directories?{query}",
                request_id=request_id,
            )
            listing = GatewayDirectoryListDTO.model_validate(remote_data)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            raise HTTPException(
                status_code=status_code if 400 <= status_code < 500 else 502,
                detail=_remote_http_error_detail(error),
            ) from error
        except (PermissionError, RuntimeError, httpx.HTTPError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return APIResponse(data=listing, request_id=request_id)

    try:
        listing = await _directory_listing(path, limit=limit)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(
            status_code=403,
            detail=str(error),
        ) from error
    except OSError as error:
        raise HTTPException(
            status_code=400,
            detail=f"读取本机目录失败: {error}",
        ) from error
    return APIResponse(data=listing, request_id=request_id)


@router.get(
    "/api/gateway/ssh-connections",
    response_model=APIResponse[SshConnectionOptionListDTO],
)
async def list_ssh_connections(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        options = await asyncio.to_thread(list_ssh_connection_options, registry)
    except (OSError, RuntimeError, ValueError) as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    return APIResponse(
        data=SshConnectionOptionListDTO(items=options),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/workspaces/local",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def add_local_workspace(
    payload: AddLocalWorkspaceRequest,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    workspace_root = Path(payload.root_path).expanduser().resolve()
    if not workspace_root.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"本机工作区不存在: {workspace_root}",
        )

    if payload.backend_url is None:
        try:
            await create_direct_managed_workspace(
                registry=registry,
                project_root=get_project_root(),
                log_dir=_gateway_root() / "logs",
                root_path=str(workspace_root),
                name=payload.name,
                create_directory=False,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
    else:
        backend_url = payload.backend_url.rstrip("/")
        gateway_config = request.app.state.gateway_config
        await wait_for_http_ok(
            f"{backend_url}/api/v1/health",
            request_timeout_seconds=(
                gateway_config.gateway_process_health_request_timeout_seconds
            ),
            poll_interval_seconds=(
                gateway_config.gateway_process_health_poll_interval_seconds
            ),
        )
        registry.upsert(
            WorkspaceTarget(
                workspace_id=build_workspace_id(
                    "local",
                    str(workspace_root),
                    backend_url,
                ),
                name=payload.name or _workspace_name(str(workspace_root)),
                name_customized=bool(payload.name),
                root_path=str(workspace_root),
                backend_url=backend_url,
                connection_kind="local",
                owner="manual",
                managed=False,
            ),
            runtime=WorkspaceRuntime(service_urls={"workspace_api": backend_url}),
            activate=False,
        )
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/remote-gateways",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def add_remote_gateway(
    payload: AddRemoteGatewayRequest,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
):
    try:
        connection = resolve_ssh_connection_request(payload, registry)
    except (LookupError, RuntimeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        gateway_config = request.app.state.gateway_config
        await register_remote_gateway(
            registry=registry,
            log_dir=_gateway_root() / "logs",
            name=payload.name,
            host=connection.host,
            port=connection.port,
            username=connection.username,
            private_key_path=connection.private_key_path,
            ssh_config_host=connection.ssh_config_host,
            remote_gateway_port=connection.remote_gateway_port,
            remote_pair_command=connection.remote_pair_command,
            activate=False,
            health_request_timeout_seconds=(
                gateway_config.gateway_process_health_request_timeout_seconds
            ),
            health_poll_interval_seconds=(
                gateway_config.gateway_process_health_poll_interval_seconds
            ),
        )
        await port_forward_manager.reconcile_workspaces()
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@router.post("/api/gateway/workspaces/ssh")
async def reject_legacy_ssh_workspace(
    _: str = Depends(verify_gateway_token),
):
    raise HTTPException(
        status_code=410,
        detail=(
            "SSH 直连 Workspace API 已移除。请调用 /api/gateway/remote-gateways，"
            "只连接远端 Gateway；remote_workspace_path 与 remote_backend_* "
            "字段不再接受。"
        ),
    )


@router.post(
    "/api/gateway/workspaces/{workspace_id}/activate",
    response_model=APIResponse[ActivateGatewayWorkspaceResultDTO],
)
async def activate_workspace(
    workspace_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        registry.activate(workspace_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=ActivateGatewayWorkspaceResultDTO(active_workspace_id=workspace_id),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/workspaces/{workspace_id}/reconnect",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def reconnect_workspace(
    workspace_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        await controller.reconnect_ssh(workspace_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (FileNotFoundError, OSError, RuntimeError, httpx.HTTPError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/workspaces/{workspace_id}/runtime/start",
    response_model=APIResponse[GatewayRuntimeStateResultDTO],
)
async def start_managed_workspace_backend(
    workspace_id: str,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        if (
            auth.kind == "federation"
            and registry.resolve(workspace_id).connection_kind != "local"
        ):
            raise ValueError("bounded federation 禁止委托嵌套远程工作区启动")
        result = await controller.start_managed_backend(
            workspace_id,
            request_id=request_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (FileNotFoundError, OSError, RuntimeError, httpx.HTTPError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/api/gateway/workspaces/{workspace_id}/runtime/stop",
    response_model=APIResponse[GatewayRuntimeStateResultDTO],
)
async def stop_managed_workspace_backend(
    workspace_id: str,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        if (
            auth.kind == "federation"
            and registry.resolve(workspace_id).connection_kind != "local"
        ):
            raise ValueError("bounded federation 禁止委托嵌套远程工作区关闭")
        result = await controller.stop_managed_backend(
            workspace_id,
            request_id=request_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/api/gateway/workspaces/{workspace_id}/runtime/restart-safe",
    response_model=APIResponse[GatewayRuntimeRestartResultDTO],
)
async def safe_restart_managed_workspace_backend(
    workspace_id: str,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        if (
            auth.kind == "federation"
            and registry.resolve(workspace_id).connection_kind != "local"
        ):
            raise ValueError("bounded federation 禁止委托嵌套远程工作区重启")
        result = await controller.safe_restart_managed_backend(
            workspace_id,
            request_id=request_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (FileNotFoundError, OSError, RuntimeError, httpx.HTTPError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/api/gateway/workspaces/{workspace_id}/runtime/restart-force",
    response_model=APIResponse[GatewayRuntimeRestartResultDTO],
)
async def force_restart_managed_workspace_backend(
    workspace_id: str,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        if (
            auth.kind == "federation"
            and registry.resolve(workspace_id).connection_kind != "local"
        ):
            raise ValueError("bounded federation 禁止委托嵌套远程工作区重启")
        result = await controller.force_restart_managed_backend(
            workspace_id,
            request_id=request_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (FileNotFoundError, OSError, RuntimeError, httpx.HTTPError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/api/gateway/workspaces/{workspace_id}/probe",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def probe_external_workspace_backend(
    workspace_id: str,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    controller: GatewayWorkspaceRuntimeController = Depends(
        get_workspace_runtime_controller
    ),
):
    try:
        if (
            auth.kind == "federation"
            and registry.resolve(workspace_id).connection_kind != "local"
        ):
            raise ValueError("bounded federation 禁止探测嵌套远程工作区")
        await controller.probe_external_backend(workspace_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (OSError, RuntimeError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@router.put(
    "/api/gateway/workspaces/order",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def reorder_workspaces(
    payload: ReorderGatewayWorkspacesRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        registry.reorder(payload.workspace_ids)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@router.patch(
    "/api/gateway/workspaces/{workspace_id}",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def update_workspace(
    workspace_id: str,
    payload: UpdateGatewayWorkspaceRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        update_fields: dict[str, str | None] = {}
        if "name" in payload.model_fields_set:
            update_fields["name"] = payload.name
        if "parent_workspace_id" in payload.model_fields_set:
            update_fields["parent_workspace_id"] = payload.parent_workspace_id
        registry.update(workspace_id, **update_fields)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@router.delete(
    "/api/gateway/workspaces/{workspace_id}",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def remove_workspace(
    workspace_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
):
    try:
        await port_forward_manager.remove_workspace(workspace_id)
        registry.remove(workspace_id, owner="manual_crud")
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )
