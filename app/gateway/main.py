from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.core.env import get_project_root, load_boxteam_env
from app.core.logging_config import configure_application_logging
from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import TraceMiddleware, get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    get_gateway_local_token,
    verify_federation_token,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.auxiliary_proxy import router as auxiliary_proxy_router
from app.gateway.config import GatewayConfig, load_gateway_config
from app.gateway.control.catalog_search import GatewaySessionCatalogSearchService
from app.gateway.control.coordinator import SessionGeneratorCoordinator
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.generators import SessionGeneratorStore
from app.gateway.control.navigation import WorkspaceNavigationStore
from app.gateway.control.resource_catalog import GatewayResourceCatalogService
from app.gateway.control.router import router as gateway_control_router
from app.gateway.control.scheduler import SessionGeneratorScheduler
from app.gateway.control.user_access import (
    USER_ACCESS_COOKIE_NAME,
    UserAccessContext,
    UserAccessService,
    UserLeaseOccupiedError,
)
from app.gateway.control.user_profile import UserProfileStore
from app.gateway.control.view_state import UserViewStateRecord, UserViewStateStore
from app.gateway.credentials import (
    FederationCredential,
    FederationCredentialStore,
    load_or_create_gateway_id,
)
from app.gateway.device_connections import router as device_connections_router
from app.gateway.diagnostics import collect_gateway_diagnostics
from app.gateway.federation import (
    FEDERATION_PROTOCOL_VERSION,
    request_remote_gateway_management,
)
from app.gateway.managed_workspaces import (
    create_direct_managed_workspace,
    list_direct_managed_workspaces,
    remove_direct_managed_workspace,
)
from app.gateway.registry import (
    GatewayWorkspaceRegistry,
    WorkspaceTarget,
)
from app.gateway.remote_gateway import (
    refresh_remote_gateway_projections,
    register_remote_gateway,
)
from app.gateway.runtime.controller import GatewayWorkspaceRuntimeController
from app.gateway.runtime.development_restart import (
    RESTART_DELAY_MS,
    resolve_development_restart_command,
    start_development_restart,
)
from app.gateway.runtime.port_forwarding import SshPortForwardManager
from app.gateway.runtime.process import (
    wait_for_http_ok,
)
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.schemas.gateway import (
    ActivateGatewayWorkspaceResultDTO,
    AddLocalWorkspaceRequest,
    AddRemoteGatewayRequest,
    AcquireGatewayUserRequest,
    CreateFederationManagedWorkspaceRequest,
    CreateGatewayGuestRequest,
    CreateGatewayManagedWorkspaceRequest,
    CreateGatewayUserRequest,
    DevelopmentRuntimeRestartDTO,
    FederationProtocolManifestDTO,
    FederationWorkspaceDTO,
    FederationWorkspaceListDTO,
    GatewayConfigSourceDTO,
    GatewayConfigSourcesDTO,
    GatewayDiagnosticsDTO,
    GatewayDirectoryEntryDTO,
    GatewayDirectoryListDTO,
    GatewayHealthDTO,
    GatewayInboundAccessListDTO,
    GatewayInboundPeerDTO,
    GatewayInboundWorkspaceDTO,
    GatewayManagedWorkspaceListDTO,
    GatewayUserAccessDTO,
    GatewayUserDTO,
    GatewayUserLeaseDTO,
    GatewayUserListDTO,
    GatewayUserViewStateDTO,
    GatewayUserViewStateUpdateRequest,
    GatewayRuntimeRestartResultDTO,
    GatewayRuntimeStateResultDTO,
    GatewayThemeCatalogDTO,
    GatewayUIAssetDTO,
    GatewayUIAssetListDTO,
    GatewayWorkspaceListDTO,
    ReorderGatewayWorkspacesRequest,
    SshConnectionOptionListDTO,
    UpdateGatewayWorkspaceRequest,
    WebUISettingsDTO,
    WebUISettingsUpdateDTO,
)
from app.gateway.server.bootstrap import create_registry
from app.gateway.server.port_forwarding import (
    get_port_forward_manager,
)
from app.gateway.server.port_forwarding import (
    router as port_forwards_router,
)
from app.gateway.server.static_ui import install_static_web_ui
from app.gateway.server.workspace_proxy import router as workspace_proxy_router
from app.gateway.ssh_connections import (
    list_ssh_connection_options,
    resolve_ssh_connection_request,
)
from app.gateway.theme import (
    MAX_UI_ASSET_BYTES,
    delete_ui_asset,
    import_ui_asset,
    list_ui_assets,
    load_validated_theme_config,
    referenced_asset_ids,
    resolve_settings_theme,
    resolve_theme,
    resolve_ui_asset,
    synchronize_theme_asset_references,
    theme_catalog,
)
from app.gateway.ui_settings import (
    merge_web_ui_settings_values,
)
from app.gateway.workspace_ids import build_workspace_id
from app.schemas.internal_v2.common import APIResponse

logger = logging.getLogger(__name__)


def _gateway_root() -> Path:
    return get_gateway_root()


def _preserve_browser_managers_on_shutdown() -> bool:
    raw = (
        os.environ.get(
            "BOXTEAM_GATEWAY_PRESERVE_BROWSER_MANAGERS_ON_SHUTDOWN",
            "true",
        )
        .strip()
        .lower()
    )
    if raw not in {"true", "false"}:
        raise RuntimeError(
            "BOXTEAM_GATEWAY_PRESERVE_BROWSER_MANAGERS_ON_SHUTDOWN "
            f"必须是 true 或 false，实际为 {raw!r}"
        )
    return raw == "true"


async def _cleanup_user_access_periodically(service: UserAccessService) -> None:
    while True:
        await asyncio.sleep(3600)
        expired_leases, expired_guests = service.cleanup_expired()
        if expired_leases or expired_guests:
            logger.info(
                "Gateway 清理过期用户访问: leases=%s guests=%s",
                expired_leases,
                expired_guests,
            )


def _resolve_local_directory(raw_path: str | None) -> Path:
    target_path = Path(raw_path).expanduser() if raw_path else Path.home()
    resolved_path = target_path.resolve()
    if not resolved_path.exists():
        raise HTTPException(status_code=400, detail=f"本机目录不存在: {resolved_path}")
    if not resolved_path.is_dir():
        raise HTTPException(status_code=400, detail=f"路径不是目录: {resolved_path}")
    return resolved_path


def _scan_local_directories(
    root_path: Path,
    limit: int,
) -> tuple[list[GatewayDirectoryEntryDTO], bool]:
    with os.scandir(root_path) as directory_iterator:
        directories = [
            entry for entry in directory_iterator if entry.is_dir(follow_symlinks=False)
        ]
    directories.sort(key=lambda entry: (entry.name.lower(), entry.name))
    entries = [
        GatewayDirectoryEntryDTO(
            name=entry.name,
            path=str(Path(entry.path).resolve()),
        )
        for entry in directories[:limit]
    ]
    return entries, len(directories) > limit


async def _directory_listing(
    raw_path: str | None,
    *,
    limit: int,
) -> GatewayDirectoryListDTO:
    root_path = _resolve_local_directory(raw_path)
    entries, truncated = await asyncio.to_thread(
        _scan_local_directories,
        root_path,
        limit,
    )
    parent_path = root_path.parent if root_path.parent != root_path else None
    return GatewayDirectoryListDTO(
        path=str(root_path),
        parent_path=str(parent_path) if parent_path is not None else None,
        home_path=str(Path.home().resolve()),
        entries=entries,
        truncated=truncated,
        limit=limit,
    )


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
    configure_application_logging()
    load_boxteam_env()
    get_gateway_local_token()
    logger.info("Gateway 日志已初始化: gateway_root=%s", _gateway_root())
    gateway_state = GatewayStateStore(path=_gateway_root() / "gateway.sqlite")
    app.state.gateway_state = gateway_state
    app.state.user_access_service = UserAccessService(state=gateway_state)
    app.state.user_profile_store = UserProfileStore(gateway_root=_gateway_root())
    app.state.user_view_state_store = UserViewStateStore(state=gateway_state)
    app.state.user_access_service.cleanup_expired()
    gateway_config = load_gateway_config(state_store=gateway_state)
    registry = await create_registry(gateway_config, state_store=gateway_state)
    app.state.registry = registry
    app.state.gateway_config = gateway_config
    app.state.port_forward_manager = SshPortForwardManager(
        registry=registry,
        storage_path=_gateway_root() / "port-forwards.json",
        log_dir=_gateway_root() / "logs",
    )
    await app.state.port_forward_manager.reconcile_workspaces()
    await app.state.port_forward_manager.restore()
    app.state.workspace_runtime_controller = GatewayWorkspaceRuntimeController(
        registry=registry,
        project_root=get_project_root(),
        log_dir=_gateway_root() / "logs",
        on_registry_reconciled=(app.state.port_forward_manager.reconcile_workspaces),
        health_request_timeout_seconds=(
            gateway_config.gateway_process_health_request_timeout_seconds
        ),
        health_poll_interval_seconds=(
            gateway_config.gateway_process_health_poll_interval_seconds
        ),
        connection_drain_timeout_seconds=(
            gateway_config.gateway_process_connection_drain_timeout_seconds
        ),
        default_skill_groups=gateway_config.default_workspace_skill_groups,
    )
    # Gateway 代理本机工作区时必须直连，不能把本地后端请求送入用户的 HTTP 代理。
    app.state.http_client = httpx.AsyncClient(timeout=None, trust_env=False)
    # SSE 会长期占用到工作区后端的连接。单独使用一个连接池，避免大量
    # 会话事件流耗尽普通 API 请求的连接额度，导致 bootstrap 等请求排队。
    app.state.streaming_http_client = httpx.AsyncClient(
        timeout=None,
        trust_env=False,
        limits=httpx.Limits(
            max_connections=1000,
            max_keepalive_connections=100,
        ),
    )
    app.state.workspace_navigation_store = WorkspaceNavigationStore(
        storage_path=_gateway_root() / "navigation" / "workspace-tree.json"
    )
    app.state.session_generator_store = SessionGeneratorStore(root=_gateway_root())
    app.state.session_generator_coordinator = SessionGeneratorCoordinator(
        registry=registry,
        store=app.state.session_generator_store,
        http_client=app.state.http_client,
    )
    app.state.session_catalog_search_service = GatewaySessionCatalogSearchService(
        registry=registry,
        http_client=app.state.http_client,
        cache_dir=_gateway_root() / "indexes" / "session-catalogs",
        navigation_store=app.state.workspace_navigation_store,
        refresh_interval_seconds=(
            gateway_config.session_catalog_refresh_interval_seconds
        ),
        max_concurrency=gateway_config.session_catalog_max_concurrency,
        request_timeout_seconds=(
            gateway_config.session_catalog_request_timeout_seconds
        ),
    )
    app.state.gateway_resource_catalog_service = GatewayResourceCatalogService(
        registry=registry,
        http_client=app.state.http_client,
    )
    app.state.session_generator_scheduler = SessionGeneratorScheduler(
        store=app.state.session_generator_store,
        coordinator=app.state.session_generator_coordinator,
        poll_interval_seconds=gateway_config.session_generator_poll_interval_seconds,
    )
    coordinator_started = False
    catalog_search_started = False
    scheduler_started = False
    user_access_cleanup_task = asyncio.create_task(
        _cleanup_user_access_periodically(app.state.user_access_service)
    )
    try:
        await app.state.session_generator_coordinator.start()
        coordinator_started = True
        await app.state.session_catalog_search_service.start()
        catalog_search_started = True
        await app.state.session_generator_scheduler.start()
        scheduler_started = True
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
        yield
    finally:
        user_access_cleanup_task.cancel()
        await asyncio.gather(user_access_cleanup_task, return_exceptions=True)
        if scheduler_started:
            logger.info("Gateway 正在停止会话生成器调度器")
            await app.state.session_generator_scheduler.stop()
        if catalog_search_started:
            logger.info("Gateway 正在停止会话目录索引同步器")
            await app.state.session_catalog_search_service.stop()
        if coordinator_started:
            logger.info("Gateway 正在停止会话生成协调器")
            await app.state.session_generator_coordinator.stop()
        logger.info("Gateway 正在关闭 HTTP 连接池")
        await app.state.http_client.aclose()
        await app.state.streaming_http_client.aclose()
        shutdown_errors: list[Exception] = []
        logger.info("Gateway 正在关闭 SSH 端口转发")
        try:
            await app.state.port_forward_manager.close()
        except Exception as error:
            shutdown_errors.append(error)
            logger.exception("Gateway 关闭 SSH 端口转发失败")
        logger.info("Gateway 正在关闭托管工作区运行时")
        try:
            registry.close(
                preserve_browser_managers=(_preserve_browser_managers_on_shutdown())
            )
        except Exception as error:
            shutdown_errors.append(error)
            logger.exception("Gateway 关闭托管工作区运行时失败")
        gateway_state.close()
        if shutdown_errors:
            raise RuntimeError(
                "Gateway lifespan 清理失败: "
                + "; ".join(str(error) for error in shutdown_errors)
            ) from shutdown_errors[0]
        logger.info("Gateway lifespan 清理完成")


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


def get_user_access_service(request: Request) -> UserAccessService:
    service = getattr(request.app.state, "user_access_service", None)
    if not isinstance(service, UserAccessService):
        raise RuntimeError("Gateway 用户访问服务尚未初始化")
    return service


def get_user_profile_store(request: Request) -> UserProfileStore:
    store = getattr(request.app.state, "user_profile_store", None)
    if not isinstance(store, UserProfileStore):
        raise RuntimeError("Gateway 用户 profile 存储尚未初始化")
    return store


def get_user_view_state_store(request: Request) -> UserViewStateStore:
    store = getattr(request.app.state, "user_view_state_store", None)
    if not isinstance(store, UserViewStateStore):
        raise RuntimeError("Gateway 用户视图状态存储尚未初始化")
    return store


def _user_access_dto(
    context: UserAccessContext,
    service: UserAccessService,
    *,
    takeover: bool = False,
) -> GatewayUserAccessDTO:
    return GatewayUserAccessDTO(
        kind=context.kind,  # type: ignore[arg-type]
        user_id=context.user_id,
        lease_generation=context.lease_generation,
        expires_at=service.expires_at(context),
        takeover=takeover,
    )


def _set_user_access_cookie(response: Response, context: UserAccessContext) -> None:
    response.set_cookie(
        key=USER_ACCESS_COOKIE_NAME,
        value=context.access_session_id,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _release_replaced_user_access(
    request: Request,
    service: UserAccessService,
    replacement: UserAccessContext,
) -> None:
    previous = service.resolve_cookie(request.cookies.get(USER_ACCESS_COOKIE_NAME))
    if previous is None or previous.access_session_id == replacement.access_session_id:
        return
    service.release(previous)


def _current_user_access(
    request: Request,
    service: UserAccessService,
) -> UserAccessContext:
    context = service.resolve_cookie(request.cookies.get(USER_ACCESS_COOKIE_NAME))
    if context is None:
        raise HTTPException(status_code=401, detail="user_session_required")
    return context


def _view_state_dto(record: UserViewStateRecord) -> GatewayUserViewStateDTO:
    return GatewayUserViewStateDTO(
        user_id=record.user_id,
        workspace_id=record.workspace_id,
        session_id=record.session_id,
        turn_anchor=record.turn_anchor,
        scroll_offset=record.scroll_offset,
        follow_latest=record.follow_latest,
        projection_version=record.projection_version,
        tool_details_expanded=record.tool_details_expanded,
        updated_at=record.updated_at,
    )


def _read_current_ui_settings(
    request: Request,
    *,
    access_service: UserAccessService,
    profiles: UserProfileStore,
) -> tuple[UserAccessContext, WebUISettingsDTO]:
    context = _current_user_access(request, access_service)
    if context.kind == "guest":
        return context, WebUISettingsDTO()
    if context.user_id is None:
        raise RuntimeError("普通用户访问上下文缺少 user_id")
    return context, profiles.read_ui_settings(user_id=context.user_id)


def _theme_asset_root(
    context: UserAccessContext,
    profiles: UserProfileStore,
) -> Path:
    if context.kind == "guest":
        # 游客不创建 profile；游客视图只能使用内置主题或网络背景。
        return _gateway_root() / "guest-theme-assets"
    if context.user_id is None:
        raise RuntimeError("普通用户访问上下文缺少 user_id")
    return profiles.theme_assets_path(user_id=context.user_id)


def _theme_config_for_access(
    context: UserAccessContext,
    *,
    profiles: UserProfileStore,
    base_config: GatewayConfig | None,
    gateway_root: Path,
) -> GatewayConfig:
    config = base_config or load_validated_theme_config(gateway_root=gateway_root)
    if context.kind == "guest":
        return profiles.guest_theme_config(config)
    if context.user_id is None:
        raise RuntimeError("普通用户访问上下文缺少 user_id")
    return profiles.theme_config(user_id=context.user_id, base_config=config)


def get_workspace_runtime_controller(
    request: Request,
) -> GatewayWorkspaceRuntimeController:
    controller = getattr(request.app.state, "workspace_runtime_controller", None)
    if not isinstance(controller, GatewayWorkspaceRuntimeController):
        raise RuntimeError("Gateway 工作区运行时控制器尚未初始化")
    return controller


@app.get("/api/gateway/health", response_model=APIResponse[GatewayHealthDTO])
async def health(
    request: Request,
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    scheduler = getattr(request.app.state, "session_generator_scheduler", None)
    if not isinstance(scheduler, SessionGeneratorScheduler):
        raise RuntimeError("会话生成器调度器尚未初始化")
    scheduler.assert_healthy()
    return APIResponse(
        data=GatewayHealthDTO(
            active_workspace_id=registry.active_workspace_id,
            process_id=os.getpid(),
            development_restart_available=(
                resolve_development_restart_command() is not None
            ),
        ),
        request_id=request_id,
    )


@app.post(
    "/api/gateway/runtime/restart-development",
    response_model=APIResponse[DevelopmentRuntimeRestartDTO],
)
async def restart_development_runtime(
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
):
    if auth.kind != "local":
        raise HTTPException(status_code=403, detail="远程 Gateway 无权重启本机开发服务")
    command = resolve_development_restart_command()
    if command is None:
        raise HTTPException(status_code=409, detail="当前不是可重启的源码开发环境")
    helper_process_id = start_development_restart(
        command,
        log_path=_gateway_root() / "logs" / "development-restart.log",
    )
    return APIResponse(
        data=DevelopmentRuntimeRestartDTO(
            previous_process_id=os.getpid(),
            helper_process_id=helper_process_id,
            delay_ms=RESTART_DELAY_MS,
        ),
        request_id=request_id,
    )


@app.get(
    "/api/gateway/config/sources",
    response_model=APIResponse[GatewayConfigSourcesDTO],
)
async def gateway_config_sources(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    try:
        gateway_state = getattr(app.state, "gateway_state", None)
        config = (
            load_gateway_config(state_store=gateway_state)
            if isinstance(gateway_state, GatewayStateStore)
            else load_gateway_config()
        )
    except (FileNotFoundError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if config.schema_path is None:
        raise RuntimeError("Gateway 配置快照缺少 schema 来源")
    return APIResponse(
        data=GatewayConfigSourcesDTO(
            revision=config.revision,
            schema_path=str(config.schema_path),
            sources=[
                GatewayConfigSourceDTO(
                    path=str(source.path),
                    layer=source.layer,
                    precedence=source.precedence,
                    loaded=source.loaded,
                )
                for source in config.source_details
            ],
        ),
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


@app.get("/api/gateway/users", response_model=APIResponse[GatewayUserListDTO])
async def list_gateway_users(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    return APIResponse(
        data=GatewayUserListDTO(
            items=[
                GatewayUserDTO(
                    user_id=record.user.user_id,
                    display_name=record.user.display_name,
                    created_at=record.user.created_at,
                    lease=GatewayUserLeaseDTO(
                        occupied=record.lease.occupied,
                        client_label=record.lease.client_label,
                        heartbeat_at=record.lease.heartbeat_at,
                        expires_at=record.lease.expires_at,
                    ),
                )
                for record in service.list_users()
            ]
        ),
        request_id=request_id,
    )


@app.post("/api/gateway/users", response_model=APIResponse[GatewayUserDTO])
async def create_gateway_user(
    payload: CreateGatewayUserRequest,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        user = service.create_user(
            display_name=payload.display_name,
            user_id=payload.user_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    try:
        gateway_config = getattr(request.app.state, "gateway_config", None)
        initial_custom_themes = (
            gateway_config.custom_themes
            if isinstance(gateway_config, GatewayConfig)
            else ()
        )
        profiles.ensure_user(
            user_id=user.user_id,
            display_name=user.display_name,
            initial_custom_themes=initial_custom_themes,
        )
    except (OSError, ValueError) as error:
        # 用户记录没有租约，创建 profile 失败时可以安全回滚，避免产生半成品用户。
        service.delete_user(user.user_id)
        raise HTTPException(status_code=500, detail=f"用户 profile 初始化失败: {error}") from error
    return APIResponse(
        data=GatewayUserDTO(
            user_id=user.user_id,
            display_name=user.display_name,
            created_at=user.created_at,
        ),
        request_id=request_id,
    )


@app.delete("/api/gateway/users/{user_id}")
async def delete_gateway_user(
    user_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        service.delete_user(user_id)
    except UserLeaseOccupiedError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "user_lease_occupied",
                "client_label": error.summary.client_label,
                "expires_at": error.summary.expires_at,
            },
        ) from error
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    try:
        profiles.delete_user(user_id=user_id)
    except OSError as error:
        raise HTTPException(status_code=500, detail=f"用户 profile 删除失败: {error}") from error
    return APIResponse(data={"user_id": user_id}, request_id=request_id)


@app.post("/api/gateway/users/{user_id}/access", response_model=APIResponse[GatewayUserAccessDTO])
async def acquire_gateway_user(
    user_id: str,
    payload: AcquireGatewayUserRequest,
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    try:
        context = service.acquire_user(
            user_id=user_id,
            client_label=payload.client_label,
        )
    except UserLeaseOccupiedError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "user_lease_occupied",
                "client_label": error.summary.client_label,
                "expires_at": error.summary.expires_at,
            },
        ) from error
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    _release_replaced_user_access(request, service, context)
    _set_user_access_cookie(response, context)
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@app.post("/api/gateway/users/{user_id}/takeover", response_model=APIResponse[GatewayUserAccessDTO])
async def takeover_gateway_user(
    user_id: str,
    payload: AcquireGatewayUserRequest,
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    try:
        context = service.acquire_user(
            user_id=user_id,
            client_label=payload.client_label,
            takeover=True,
        )
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    _release_replaced_user_access(request, service, context)
    _set_user_access_cookie(response, context)
    return APIResponse(
        data=_user_access_dto(context, service, takeover=True),
        request_id=request_id,
    )


@app.post("/api/gateway/users/guest", response_model=APIResponse[GatewayUserAccessDTO])
async def acquire_gateway_guest(
    payload: CreateGatewayGuestRequest,
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    context = service.acquire_guest(tracking=payload.tracking)
    _release_replaced_user_access(request, service, context)
    _set_user_access_cookie(response, context)
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@app.get("/api/gateway/users/current", response_model=APIResponse[GatewayUserAccessDTO])
async def current_gateway_user(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    context = _current_user_access(request, service)
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@app.post("/api/gateway/users/current/heartbeat", response_model=APIResponse[GatewayUserAccessDTO])
async def heartbeat_gateway_user(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    context = _current_user_access(request, service)
    try:
        context = service.heartbeat(context)
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=_user_access_dto(context, service),
        request_id=request_id,
    )


@app.delete("/api/gateway/users/current")
async def release_gateway_user(
    request: Request,
    response: Response,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
):
    context = _current_user_access(request, service)
    service.release(context)
    response.delete_cookie(key=USER_ACCESS_COOKIE_NAME, path="/")
    return APIResponse(data={"released": True}, request_id=request_id)


@app.get("/api/gateway/users/current/view-state")
async def get_gateway_user_view_state(
    request: Request,
    workspace_id: str = Query(min_length=1, max_length=256),
    session_id: str = Query(min_length=1, max_length=256),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    store: UserViewStateStore = Depends(get_user_view_state_store),
):
    context = _current_user_access(request, service)
    try:
        record = store.get(
            context=context,
            workspace_id=workspace_id,
            session_id=session_id,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return APIResponse(
        data=_view_state_dto(record) if record is not None else None,
        request_id=request_id,
    )


@app.get("/api/gateway/users/current/view-state/latest")
async def get_latest_gateway_user_view_state(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    store: UserViewStateStore = Depends(get_user_view_state_store),
):
    context = _current_user_access(request, service)
    try:
        record = store.get_latest(context=context)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return APIResponse(
        data=_view_state_dto(record) if record is not None else None,
        request_id=request_id,
    )


@app.put("/api/gateway/users/current/view-state")
async def put_gateway_user_view_state(
    payload: GatewayUserViewStateUpdateRequest,
    request: Request,
    workspace_id: str = Query(min_length=1, max_length=256),
    session_id: str = Query(min_length=1, max_length=256),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    service: UserAccessService = Depends(get_user_access_service),
    store: UserViewStateStore = Depends(get_user_view_state_store),
):
    context = _current_user_access(request, service)
    try:
        record = store.put(
            context=context,
            workspace_id=workspace_id,
            session_id=session_id,
            turn_anchor=payload.turn_anchor,
            scroll_offset=payload.scroll_offset,
            follow_latest=payload.follow_latest,
            projection_version=payload.projection_version,
            tool_details_expanded=payload.tool_details_expanded,
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return APIResponse(data=_view_state_dto(record), request_id=request_id)


@app.get("/api/gateway/workspaces", response_model=APIResponse[GatewayWorkspaceListDTO])
async def list_workspaces(
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
                "diagnostics_logs",
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
        (f"{target.workspace_id}: bounded federation 不导出从其他 Gateway 导入的工作区")
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
    "/api/gateway/federation/diagnostics",
    response_model=APIResponse[GatewayDiagnosticsDTO],
)
async def federation_diagnostics(
    remote_workspace_id: str | None = Query(default=None),
    log_id: str | None = Query(default=None),
    tail_lines: int = Query(default=300, ge=20, le=1000),
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        result = await collect_gateway_diagnostics(
            registry,
            selected_workspace_id=remote_workspace_id,
            selected_log_id=log_id,
            tail_lines=tail_lines,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


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
    "/api/gateway/federation/directories",
    response_model=APIResponse[GatewayDirectoryListDTO],
)
async def list_federation_directories(
    path: str | None = Query(default=None),
    limit: int = Query(default=120, ge=1, le=500),
    _: object = Depends(verify_federation_token),
    request_id: str = Depends(get_request_id),
):
    try:
        listing = await _directory_listing(path, limit=limit)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(
            status_code=400,
            detail=f"读取远程 Gateway 目录失败: {error}",
        ) from error
    return APIResponse(data=listing, request_id=request_id)


@app.get(
    "/api/gateway/managed-workspaces",
    response_model=APIResponse[GatewayManagedWorkspaceListDTO],
)
async def gateway_managed_workspaces(
    gateway_connection_id: str | None = Query(default=None),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
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
            await port_forward_manager.reconcile_workspaces()
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
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
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
            await port_forward_manager.reconcile_workspaces()
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
    port_forward_manager: SshPortForwardManager = Depends(get_port_forward_manager),
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
            await port_forward_manager.reconcile_workspaces()
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
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        theme_asset_root = _theme_asset_root(context, profiles)
        theme_config = _theme_config_for_access(
            context,
            profiles=profiles,
            base_config=getattr(request.app.state, "gateway_config", None),
            gateway_root=theme_asset_root,
        )
        settings = resolve_settings_theme(
            user_settings,
            config=load_validated_theme_config(
                gateway_root=theme_asset_root,
                config=theme_config,
            ),
            gateway_root=theme_asset_root,
        )
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=settings, request_id=request_id)


@app.put("/api/gateway/ui-settings", response_model=APIResponse[WebUISettingsDTO])
async def update_web_ui_settings(
    payload: WebUISettingsUpdateDTO,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, current_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        theme_asset_root = _theme_asset_root(context, profiles)
        config = load_validated_theme_config(
            gateway_root=theme_asset_root,
            config=_theme_config_for_access(
                context,
                profiles=profiles,
                base_config=getattr(request.app.state, "gateway_config", None),
                gateway_root=theme_asset_root,
            ),
        )
        if payload.theme is not None:
            theme_id = (
                payload.theme.theme_id
                or current_settings.theme.theme_id
                or config.default_theme_id
            )
            background = (
                payload.theme.background
                if "background" in payload.theme.model_fields_set
                else current_settings.theme.background
            )
            resolve_theme(
                theme_id,
                config=config,
                gateway_root=theme_asset_root,
                background_override=background,
            )
        updated = merge_web_ui_settings_values(current_settings, payload)
        if context.kind == "user":
            if context.user_id is None:
                raise RuntimeError("普通用户访问上下文缺少 user_id")
            profiles.write_ui_settings(user_id=context.user_id, settings=updated)
        resolved = resolve_settings_theme(
            updated,
            config=config,
            gateway_root=theme_asset_root,
        )
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=resolved, request_id=request_id)


@app.get("/api/gateway/themes", response_model=APIResponse[GatewayThemeCatalogDTO])
async def get_gateway_themes(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        theme_asset_root = _theme_asset_root(context, profiles)
        theme_config = _theme_config_for_access(
            context,
            profiles=profiles,
            base_config=getattr(request.app.state, "gateway_config", None),
            gateway_root=theme_asset_root,
        )
        catalog = theme_catalog(
            user_settings,
            config=load_validated_theme_config(
                gateway_root=theme_asset_root,
                config=theme_config,
            ),
            gateway_root=theme_asset_root,
        )
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=catalog, request_id=request_id)


@app.get("/api/gateway/ui-assets", response_model=APIResponse[GatewayUIAssetListDTO])
async def get_gateway_ui_assets(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        if context.kind == "guest":
            assets = []
        else:
            theme_asset_root = _theme_asset_root(context, profiles)
            theme_config = _theme_config_for_access(
                context,
                profiles=profiles,
                base_config=getattr(request.app.state, "gateway_config", None),
                gateway_root=theme_asset_root,
            )
            synchronize_theme_asset_references(
                load_validated_theme_config(
                    gateway_root=theme_asset_root,
                    config=theme_config,
                ),
                user_settings,
                gateway_root=theme_asset_root,
            )
            assets = list_ui_assets(theme_asset_root)
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=GatewayUIAssetListDTO(items=assets),
        request_id=request_id,
    )


@app.post("/api/gateway/ui-assets", response_model=APIResponse[GatewayUIAssetDTO])
async def upload_gateway_ui_asset(
    request: Request,
    file: UploadFile = File(),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    context = _current_user_access(request, access_service)
    if context.kind == "guest":
        raise HTTPException(status_code=403, detail="游客不能上传主题资源")
    content = await file.read(MAX_UI_ASSET_BYTES + 1)
    try:
        asset = import_ui_asset(
            content,
            original_filename=file.filename or "background",
            gateway_root=_theme_asset_root(context, profiles),
            declared_content_type=file.content_type,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=asset, request_id=request_id)


@app.get("/api/gateway/ui-assets/{asset_id}", response_class=FileResponse)
async def get_gateway_ui_asset(
    asset_id: str,
    request: Request,
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    context = _current_user_access(request, access_service)
    try:
        path, asset = resolve_ui_asset(
            asset_id,
            gateway_root=_theme_asset_root(context, profiles),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (FileNotFoundError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(
        path,
        media_type=asset.content_type,
        filename=asset.original_filename,
        headers={
            "ETag": f'"{asset.sha256}"',
            "Cache-Control": "public, max-age=31536000, immutable",
        },
        content_disposition_type="inline",
    )


@app.delete(
    "/api/gateway/ui-assets/{asset_id}",
    response_model=APIResponse[GatewayUIAssetListDTO],
)
async def remove_gateway_ui_asset(
    asset_id: str,
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
    access_service: UserAccessService = Depends(get_user_access_service),
    profiles: UserProfileStore = Depends(get_user_profile_store),
):
    try:
        context, user_settings = _read_current_ui_settings(
            request,
            access_service=access_service,
            profiles=profiles,
        )
        if context.kind == "guest":
            raise HTTPException(status_code=403, detail="游客不能删除主题资源")
        theme_asset_root = _theme_asset_root(context, profiles)
        theme_config = _theme_config_for_access(
            context,
            profiles=profiles,
            base_config=getattr(request.app.state, "gateway_config", None),
            gateway_root=theme_asset_root,
        )
        references = referenced_asset_ids(
            load_validated_theme_config(
                gateway_root=theme_asset_root,
                config=theme_config,
            ),
            user_settings,
            gateway_root=theme_asset_root,
        ).get(asset_id, [])
    except (OSError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if references:
        raise HTTPException(
            status_code=409,
            detail=f"背景资源正在被主题引用，不能删除: {', '.join(references)}",
        )
    try:
        delete_ui_asset(asset_id, gateway_root=theme_asset_root)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=GatewayUIAssetListDTO(items=list_ui_assets(theme_asset_root)),
        request_id=request_id,
    )


@app.get(
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


@app.post(
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


@app.post(
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
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (FileNotFoundError, OSError, RuntimeError, httpx.HTTPError) as error:
        registry.mark_connection_error(workspace_id, str(error))
        raise HTTPException(status_code=502, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@app.post(
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


@app.put(
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


@app.delete(
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
app.include_router(gateway_control_router)
app.include_router(device_connections_router)
app.include_router(port_forwards_router)
app.include_router(auxiliary_proxy_router)
app.include_router(workspace_proxy_router)

# 静态 UI 必须最后挂载，确保 Gateway API、工作区代理、SSE 和 WebSocket
# 路由优先匹配；源码开发未声明 BOXTEAM_WEB_ASSETS 时由 Vite 提供页面。
install_static_web_ui(app)
