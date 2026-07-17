from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.env import get_project_root, load_project_env
from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import TraceMiddleware, get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.auxiliary_proxy import router as auxiliary_proxy_router
from app.gateway.local_workspace import start_managed_local_workspace_runtime
from app.gateway.processes import (
    wait_for_http_ok,
)
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceTarget,
)
from app.gateway.remote_files import list_ssh_directories
from app.gateway.server.bootstrap import create_registry
from app.gateway.server.workspace_proxy import router as workspace_proxy_router
from app.gateway.ssh_connections import (
    list_ssh_connection_options,
    resolve_directory_connection,
    resolve_ssh_connection_request,
)
from app.gateway.schemas import (
    ActivateGatewayWorkspaceResultDTO,
    AddLocalWorkspaceRequest,
    AddSshWorkspaceRequest,
    GatewayHealthDTO,
    GatewayWorkspaceListDTO,
    GatewayDirectoryEntryDTO,
    GatewayDirectoryListDTO,
    RenameGatewayWorkspaceRequest,
    ReorderGatewayWorkspacesRequest,
    SshConnectionOptionListDTO,
    WebUISettingsDTO,
    WebUISettingsUpdateDTO,
)
from app.gateway.ssh_workspace import register_ssh_workspace
from app.gateway.service_runtime import WorkspaceRuntime
from app.gateway.ui_settings import (
    merge_web_ui_settings,
    read_web_ui_settings,
)
from app.gateway.workspace_ids import build_workspace_id
from app.gateway.workspace_reconnect import reconnect_gateway_workspace
from app.schemas.public_v2.common import APIResponse


def _gateway_root() -> Path:
    return get_gateway_root()


def _resolve_local_directory(raw_path: str | None) -> Path:
    target_path = Path(raw_path).expanduser() if raw_path else Path.home()
    resolved_path = target_path.resolve()
    if not resolved_path.exists():
        raise HTTPException(status_code=400, detail=f"本机目录不存在: {resolved_path}")
    if not resolved_path.is_dir():
        raise HTTPException(status_code=400, detail=f"路径不是目录: {resolved_path}")
    return resolved_path


def _workspace_name(root_path: str, fallback: str = "workspace") -> str:
    name = Path(root_path).name
    return name or fallback


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_project_env()
    registry = await create_registry()
    app.state.registry = registry
    app.state.http_client = httpx.AsyncClient(timeout=None)
    app.state.attach_frontend_urls = {
        "terminal": os.environ.get(
            "BOXTEAM_TERMINAL_FRONTEND_URL",
            "http://127.0.0.1:8013",
        ).rstrip("/"),
        "browser": os.environ.get(
            "BOXTEAM_BROWSER_FRONTEND_URL",
            "http://127.0.0.1:8016",
        ).rstrip("/"),
    }
    try:
        yield
    finally:
        await app.state.http_client.aclose()
        registry.close()


app = FastAPI(
    title="BoxTeam Workspace Gateway",
    version="1.0.0",
    docs_url="/api/gateway/docs",
    openapi_url="/api/gateway/openapi.json",
    redoc_url="/api/gateway/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TraceMiddleware)
app.include_router(auxiliary_proxy_router)
app.include_router(workspace_proxy_router)


def get_registry(request: Request) -> GatewayWorkspaceRegistry:
    registry = getattr(request.app.state, "registry", None)
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise RuntimeError("Gateway registry 尚未初始化")
    return registry


@app.get("/api/gateway/health", response_model=APIResponse[GatewayHealthDTO])
async def health(
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    return APIResponse(
        data=GatewayHealthDTO(active_workspace_id=registry.active_workspace_id),
        request_id=request_id,
    )

@app.get("/api/gateway/workspaces", response_model=APIResponse[GatewayWorkspaceListDTO])
async def list_workspaces(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@app.get("/api/gateway/ui-settings", response_model=APIResponse[WebUISettingsDTO])
async def get_web_ui_settings(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    return APIResponse(
        data=read_web_ui_settings(_gateway_root()),
        request_id=request_id,
    )


@app.put("/api/gateway/ui-settings", response_model=APIResponse[WebUISettingsDTO])
async def update_web_ui_settings(
    payload: WebUISettingsUpdateDTO,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    return APIResponse(
        data=merge_web_ui_settings(payload, gateway_root=_gateway_root()),
        request_id=request_id,
    )


@app.get("/api/gateway/local-directories", response_model=APIResponse[GatewayDirectoryListDTO])
async def list_local_directories(
    path: str | None = Query(default=None, description="要浏览的本机目录；为空时使用用户主目录"),
    limit: int = Query(default=120, ge=1, le=500),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    root_path = _resolve_local_directory(path)
    entries: list[GatewayDirectoryEntryDTO] = []
    with os.scandir(root_path) as directory_iterator:
        directory_entries = list(directory_iterator)
    directories = [
        entry
        for entry in directory_entries
        if entry.is_dir(follow_symlinks=False)
    ]
    directories.sort(key=lambda entry: (entry.name.lower(), entry.name))
    for entry in directories[:limit]:
        entry_path = Path(entry.path).resolve()
        entries.append(
            GatewayDirectoryEntryDTO(
                name=entry.name,
                path=str(entry_path),
            )
        )
    parent_path = root_path.parent if root_path.parent != root_path else None
    return APIResponse(
        data=GatewayDirectoryListDTO(
            path=str(root_path),
            parent_path=str(parent_path) if parent_path is not None else None,
            home_path=str(Path.home().resolve()),
            entries=entries,
            truncated=len(directories) > limit,
            limit=limit,
        ),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/ssh-connections/{connection_id}/directories",
    response_model=APIResponse[GatewayDirectoryListDTO],
)
async def list_remote_directories(
    connection_id: str,
    path: str | None = Query(default=None, description="要浏览的远程目录；为空时使用远程用户主目录"),
    limit: int = Query(default=120, ge=1, le=500),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        connection = resolve_directory_connection(connection_id, registry)
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        listing = await list_ssh_directories(connection, path, limit)
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(data=listing, request_id=request_id)


@app.get(
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


@app.post("/api/gateway/workspaces/local", response_model=APIResponse[GatewayWorkspaceListDTO])
async def add_local_workspace(
    payload: AddLocalWorkspaceRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    workspace_root = Path(payload.root_path).expanduser().resolve()
    if not workspace_root.is_dir():
        raise HTTPException(status_code=400, detail=f"本机工作区不存在: {workspace_root}")

    backend_url = payload.backend_url.rstrip("/") if payload.backend_url else None
    managed_runtime = None
    if backend_url is None:
        project_root = get_project_root()
        managed_runtime = await start_managed_local_workspace_runtime(
            project_root=project_root,
            workspace_root=workspace_root,
            log_dir=_gateway_root() / "logs",
        )
        backend_url = managed_runtime.service_urls["workspace_api"]
    else:
        await wait_for_http_ok(f"{backend_url}/api/v1/health")
        managed_runtime = WorkspaceRuntime(
            service_urls={"workspace_api": backend_url}
        )

    workspace_id = build_workspace_id(
        "local",
        str(workspace_root),
        backend_url,
    )
    registry.upsert(
        WorkspaceTarget(
            workspace_id=workspace_id,
            name=payload.name or _workspace_name(str(workspace_root)),
            name_customized=bool(payload.name),
            root_path=str(workspace_root),
            backend_url=backend_url,
            connection_kind="local",
            managed=payload.backend_url is None,
        ),
        runtime=managed_runtime,
        activate=False,
    )
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@app.post("/api/gateway/workspaces/ssh", response_model=APIResponse[GatewayWorkspaceListDTO])
async def add_ssh_workspace(
    payload: AddSshWorkspaceRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        connection = resolve_ssh_connection_request(payload, registry)
    except (LookupError, RuntimeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        await register_ssh_workspace(
            registry=registry,
            log_dir=_gateway_root() / "logs",
            name=payload.name,
            host=connection.host,
            port=connection.port,
            username=connection.username,
            private_key_path=connection.private_key_path,
            ssh_config_host=connection.ssh_config_host,
            remote_backend_host=connection.remote_backend_host,
            remote_backend_port=connection.remote_backend_port,
            remote_services=connection.remote_services,
            remote_workspace_path=payload.remote_workspace_path,
            activate=False,
            name_customized=bool(payload.name),
        )
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@app.post(
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


@app.post(
    "/api/gateway/workspaces/{workspace_id}/reconnect",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def reconnect_workspace(
    workspace_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        await reconnect_gateway_workspace(
            registry=registry,
            workspace_id=workspace_id,
            project_root=get_project_root(),
            log_dir=_gateway_root() / "logs",
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@app.put("/api/gateway/workspaces/order", response_model=APIResponse[GatewayWorkspaceListDTO])
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


@app.patch(
    "/api/gateway/workspaces/{workspace_id}",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def rename_workspace(
    workspace_id: str,
    payload: RenameGatewayWorkspaceRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        registry.rename(workspace_id, payload.name)
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


@app.delete("/api/gateway/workspaces/{workspace_id}", response_model=APIResponse[GatewayWorkspaceListDTO])
async def remove_workspace(
    workspace_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        registry.remove(workspace_id)
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
