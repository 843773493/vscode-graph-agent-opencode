from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.env import get_project_root, load_project_env
from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import TraceMiddleware, get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    get_gateway_local_token,
    verify_federation_token,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.credentials import (
    FederationCredential,
    FederationCredentialStore,
    load_or_create_gateway_id,
)
from app.gateway.federation import (
    FEDERATION_PROTOCOL_VERSION,
    request_remote_gateway_management,
)
from app.gateway.managed_workspaces import (
    create_direct_managed_workspace,
    list_direct_managed_workspaces,
    remove_direct_managed_workspace,
)
from app.gateway.auxiliary_proxy import router as auxiliary_proxy_router
from app.gateway.runtime.process import (
    wait_for_http_ok,
)
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceTarget,
)
from app.gateway.server.bootstrap import create_registry
from app.gateway.server.workspace_proxy import router as workspace_proxy_router
from app.gateway.server.static_ui import install_static_web_ui
from app.gateway.ssh_connections import (
    list_ssh_connection_options,
    resolve_ssh_connection_request,
)
from app.gateway.schemas import (
    ActivateGatewayWorkspaceResultDTO,
    AddLocalWorkspaceRequest,
    AddRemoteGatewayRequest,
    CreateFederationManagedWorkspaceRequest,
    CreateGatewayManagedWorkspaceRequest,
    FederationProtocolManifestDTO,
    FederationWorkspaceDTO,
    FederationWorkspaceListDTO,
    GatewayHealthDTO,
    GatewayInboundAccessListDTO,
    GatewayInboundPeerDTO,
    GatewayInboundWorkspaceDTO,
    GatewayManagedWorkspaceListDTO,
    GatewayRuntimeRestartResultDTO,
    GatewayWorkspaceListDTO,
    GatewayDirectoryEntryDTO,
    GatewayDirectoryListDTO,
    UpdateGatewayWorkspaceRequest,
    ReorderGatewayWorkspacesRequest,
    SshConnectionOptionListDTO,
    WebUISettingsDTO,
    WebUISettingsUpdateDTO,
)
from app.gateway.remote_gateway import (
    refresh_remote_gateway_projections,
    register_remote_gateway,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.gateway.ui_settings import (
    merge_web_ui_settings,
    read_web_ui_settings,
)
from app.gateway.workspace_ids import build_workspace_id
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
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


async def _managed_workspace_list(
    registry: GatewayWorkspaceRegistry,
) -> GatewayManagedWorkspaceListDTO:
    return GatewayManagedWorkspaceListDTO(
        gateway_id=load_or_create_gateway_id(_gateway_root() / "identity.json"),
        gateway_name="本机 Gateway",
        connection_kind="local",
        items=await list_direct_managed_workspaces(registry),
    )


async def _inbound_gateway_access_list(
    registry: GatewayWorkspaceRegistry,
) -> GatewayInboundAccessListDTO:
    gateway_id = load_or_create_gateway_id(_gateway_root() / "identity.json")
    credentials = FederationCredentialStore(
        storage_path=_gateway_root() / "credentials" / "federation.json"
    ).list_valid()
    peers = [
        GatewayInboundPeerDTO(
            connection_id=credential.connection_id,
            peer_gateway_id=credential.peer_gateway_id,
            credential_expires_at=credential.expires_at.isoformat(),
        )
        for credential in credentials
        if credential.peer_gateway_id != gateway_id
    ]
    workspaces = [
        GatewayInboundWorkspaceDTO(
            workspace_id=workspace.workspace_id,
            name=workspace.name,
            root_path=workspace.root_path,
            status=workspace.status,
            managed=workspace.managed,
            system_default=workspace.system_default,
        )
        for workspace in await registry.list_dtos()
        if workspace.connection_kind == "local"
    ]
    return GatewayInboundAccessListDTO(
        gateway_id=gateway_id,
        peers=peers,
        items=workspaces if peers else [],
    )


def _remote_gateway_credential(connection_id: str) -> FederationCredential:
    return FederationCredentialStore(
        storage_path=_gateway_root() / "credentials" / "federation.json"
    ).get(connection_id)


def _remote_managed_workspace_list(
    registry: GatewayWorkspaceRegistry,
    connection_id: str,
    remote_data: dict[str, object],
) -> GatewayManagedWorkspaceListDTO:
    connection = registry.remote_gateway_connection(connection_id)
    remote_result = GatewayManagedWorkspaceListDTO.model_validate(remote_data)
    return remote_result.model_copy(
        update={
            "gateway_connection_id": connection_id,
            "gateway_name": connection.name,
            "connection_kind": "remote_gateway",
        }
    )


def _remote_http_error_detail(error: httpx.HTTPStatusError) -> str:
    try:
        payload = error.response.json()
    except ValueError:
        return error.response.text[:1000]
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
        return payload["detail"]
    return error.response.text[:1000]


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_project_env()
    get_gateway_local_token()
    registry = await create_registry()
    app.state.registry = registry
    app.state.workspace_runtime_controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=get_project_root(),
        log_dir=_gateway_root() / "logs",
    )
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


def get_registry(request: Request) -> GatewayWorkspaceRegistry:
    registry = getattr(request.app.state, "registry", None)
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise RuntimeError("Gateway registry 尚未初始化")
    return registry


def get_workspace_runtime_controller(
    request: Request,
) -> GatewayWorkspaceRuntimeController:
    controller = getattr(request.app.state, "workspace_runtime_controller", None)
    if not isinstance(controller, GatewayWorkspaceRuntimeController):
        raise RuntimeError("Gateway 工作区运行时控制器尚未初始化")
    return controller


@app.get("/api/gateway/health", response_model=APIResponse[GatewayHealthDTO])
async def health(
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    return APIResponse(
        data=GatewayHealthDTO(active_workspace_id=registry.active_workspace_id),
        request_id=request_id,
    )


@app.get("/api/gateway/auth/local-credential")
async def local_credential(
    request: Request,
    request_id: str = Depends(get_request_id),
):
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site not in {None, "same-origin", "same-site"}:
        raise HTTPException(
            status_code=403,
            detail="Gateway 本地凭据只允许同站点 Web UI 获取",
        )
    return APIResponse(
        data={"token": get_gateway_local_token()},
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


@app.get(
    "/api/gateway/federation/manifest",
    response_model=APIResponse[FederationProtocolManifestDTO],
)
async def federation_manifest(
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
):
    return APIResponse(
        data=FederationProtocolManifestDTO(
            protocol_version=FEDERATION_PROTOCOL_VERSION,
            gateway_id=load_or_create_gateway_id(_gateway_root() / "identity.json"),
            capabilities=[
                "workspace_discovery",
                "workspace_proxy",
                "auxiliary_proxy",
                "managed_backend_restart",
                "managed_workspace_admin",
            ],
        ),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/federation/workspaces",
    response_model=APIResponse[FederationWorkspaceListDTO],
)
async def federation_workspaces(
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    def services_for(workspace_id: str) -> list[str]:
        services = ["workspace_api"]
        for service, public_name in (
            ("terminal_manager", "terminal_manager"),
            ("browser_manager", "browser_manager"),
        ):
            try:
                registry.resolve_service_url(workspace_id, service)
            except LookupError:
                continue
            services.append(public_name)
        return services

    direct = [
        FederationWorkspaceDTO(
            workspace_id=target.workspace_id,
            name=target.name,
            root_path=target.root_path,
            managed=target.managed,
            connection_kind="local",
            services=services_for(target.workspace_id),
        )
        for target in registry.targets()
        if target.connection_kind == "local"
    ]
    excluded = [
        (
            f"{target.workspace_id}: bounded federation "
            "不导出从其他 Gateway 导入的工作区"
        )
        for target in registry.targets()
        if target.connection_kind == "remote_gateway"
    ]
    return APIResponse(
        data=FederationWorkspaceListDTO(
            protocol_version=FEDERATION_PROTOCOL_VERSION,
            gateway_id=load_or_create_gateway_id(_gateway_root() / "identity.json"),
            items=direct,
            excluded=excluded,
        ),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/federation/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def federation_managed_workspaces(
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/federation/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def create_federation_managed_workspace(
    payload: CreateFederationManagedWorkspaceRequest,
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        await create_direct_managed_workspace(
            registry=registry,
            project_root=get_project_root(),
            log_dir=_gateway_root() / "logs",
            root_path=payload.root_path,
            name=payload.name,
            create_directory=payload.create_directory,
        )
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@app.delete(
    "/api/gateway/federation/managed-workspaces/{workspace_id}",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def remove_federation_managed_workspace(
    workspace_id: str,
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        remove_direct_managed_workspace(registry, workspace_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def gateway_managed_workspaces(
    gateway_connection_id: str | None = Query(default=None),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    if gateway_connection_id is not None:
        try:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(gateway_connection_id),
                credential=_remote_gateway_credential(gateway_connection_id),
                method="GET",
                path="/api/gateway/federation/managed-workspaces",
                request_id=request_id,
            )
            await refresh_remote_gateway_projections(
                registry=registry,
                connection_id=gateway_connection_id,
            )
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
        return APIResponse(
            data=_remote_managed_workspace_list(
                registry,
                gateway_connection_id,
                remote_data,
            ),
            request_id=request_id,
        )
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/inbound-access",
    response_model=APIResponse[GatewayInboundAccessListDTO],
)
async def gateway_inbound_access(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    return APIResponse(
        data=await _inbound_gateway_access_list(registry),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def create_gateway_managed_workspace(
    payload: CreateGatewayManagedWorkspaceRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    connection_id = payload.gateway_connection_id
    remote_data: dict[str, object] | None = None
    try:
        if connection_id is None:
            await create_direct_managed_workspace(
                registry=registry,
                project_root=get_project_root(),
                log_dir=_gateway_root() / "logs",
                root_path=payload.root_path,
                name=payload.name,
                create_directory=payload.create_directory,
            )
        else:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(connection_id),
                credential=_remote_gateway_credential(connection_id),
                method="POST",
                path="/api/gateway/federation/managed-workspaces",
                request_id=request_id,
                payload={
                    "root_path": payload.root_path,
                    "name": payload.name,
                    "create_directory": payload.create_directory,
                },
            )
            await refresh_remote_gateway_projections(
                registry=registry,
                connection_id=connection_id,
            )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        raise HTTPException(
            status_code=status_code if 400 <= status_code < 500 else 502,
            detail=_remote_http_error_detail(error),
        ) from error
    except (PermissionError, OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if connection_id is not None and remote_data is not None:
        return APIResponse(
            data=_remote_managed_workspace_list(
                registry,
                connection_id,
                remote_data,
            ),
            request_id=request_id,
        )
    return APIResponse(
        data=await _managed_workspace_list(registry),
        request_id=request_id,
    )


@app.delete(
    "/api/gateway/managed-workspaces/{workspace_id}",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def remove_gateway_managed_workspace(
    workspace_id: str,
    gateway_connection_id: str | None = Query(default=None),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    remote_data: dict[str, object] | None = None
    try:
        if gateway_connection_id is None:
            remove_direct_managed_workspace(registry, workspace_id)
        else:
            remote_data = await request_remote_gateway_management(
                gateway_url=registry.remote_gateway_url(gateway_connection_id),
                credential=_remote_gateway_credential(gateway_connection_id),
                method="DELETE",
                path=(
                    "/api/gateway/federation/managed-workspaces/"
                    f"{quote(workspace_id, safe='')}"
                ),
                request_id=request_id,
            )
            await refresh_remote_gateway_projections(
                registry=registry,
                connection_id=gateway_connection_id,
            )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        raise HTTPException(
            status_code=status_code if 400 <= status_code < 500 else 502,
            detail=_remote_http_error_detail(error),
        ) from error
    except (OSError, RuntimeError, httpx.HTTPError) as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if gateway_connection_id is not None and remote_data is not None:
        return APIResponse(
            data=_remote_managed_workspace_list(
                registry,
                gateway_connection_id,
                remote_data,
            ),
            request_id=request_id,
        )
    return APIResponse(
        data=await _managed_workspace_list(registry),
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
        await wait_for_http_ok(f"{backend_url}/api/v1/health")
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
                managed=False,
            ),
            runtime=WorkspaceRuntime(
                service_urls={"workspace_api": backend_url}
            ),
            activate=False,
        )
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        ),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/remote-gateways",
    response_model=APIResponse[GatewayWorkspaceListDTO],
)
async def add_remote_gateway(
    payload: AddRemoteGatewayRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        connection = resolve_ssh_connection_request(payload, registry)
    except (LookupError, RuntimeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
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
            activate=False,
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


@app.post("/api/gateway/workspaces/ssh")
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


@app.post(
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


@app.post(
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


@app.post(
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


# 两个代理 Router 含通配路由，必须晚于 Gateway 自有接口注册，否则会吞掉
# `/api/gateway/workspaces/{id}/runtime/*` 等更具体的控制面路由。
app.include_router(auxiliary_proxy_router)
app.include_router(workspace_proxy_router)

# 静态 UI 必须最后挂载，确保 Gateway API、工作区代理、SSE 和 WebSocket
# 路由优先匹配；源码开发未声明 BOXTEAM_WEB_ASSETS 时由 Vite 提供页面。
install_static_web_ui(app)
