from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.core.env import get_project_root, load_project_env
from app.core.path_utils import get_user_workspace_root
from app.core.trace_middleware import TraceMiddleware, get_request_id
from app.gateway.config import load_gateway_config, resolve_gateway_path
from app.gateway.processes import (
    allocate_local_port,
    allocate_ssh_tunnel_port,
    start_local_backend_process,
    start_ssh_tunnel_process,
    wait_for_http_ok,
)
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.schemas import (
    ActivateGatewayWorkspaceResultDTO,
    AddLocalWorkspaceRequest,
    AddSshWorkspaceRequest,
    GatewayHealthDTO,
    GatewayWorkspaceListDTO,
    LocalDirectoryEntryDTO,
    LocalDirectoryListDTO,
    ReorderGatewayWorkspacesRequest,
    WebUISettingsDTO,
    WebUISettingsUpdateDTO,
)
from app.gateway.ui_settings import (
    merge_web_ui_settings,
    read_web_ui_settings,
)
from app.schemas.public_v2.common import APIResponse


LOCAL_TOKEN = "local-dev-token"
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def verify_gateway_token(x_local_token: str | None = Header(default=None)) -> str:
    if x_local_token != LOCAL_TOKEN:
        raise HTTPException(status_code=401, detail="invalid local token")
    return x_local_token


def _gateway_root() -> Path:
    configured = os.environ.get("BOXTEAM_GATEWAY_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    workspace_root = os.environ.get("WORKSPACE_ROOT")
    if workspace_root:
        return Path(workspace_root).expanduser().resolve() / ".boxteam" / "gateway"
    return get_user_workspace_root() / ".boxteam" / "gateway"


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


def _default_workspace_root() -> Path | None:
    configured_root = os.environ.get("BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT")
    if configured_root:
        root_path = Path(configured_root).expanduser().resolve()
    else:
        root_path = get_user_workspace_root()
    root_path.mkdir(parents=True, exist_ok=True)
    return root_path


def _gateway_config_workspace_root(default_root_path: Path | None) -> Path | None:
    configured_root = os.environ.get("BOXTEAM_GATEWAY_CONFIG_WORKSPACE_ROOT")
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    raw_runtime_root = os.environ.get("WORKSPACE_ROOT")
    if raw_runtime_root:
        return Path(raw_runtime_root).expanduser().resolve()
    return default_root_path


async def _register_ssh_workspace(
    *,
    registry: GatewayWorkspaceRegistry,
    name: str | None,
    host: str,
    port: int,
    username: str,
    private_key_path: str,
    remote_backend_host: str,
    remote_backend_port: int,
    remote_workspace_path: str,
    activate: bool,
) -> WorkspaceTarget:
    resolved_private_key_path = resolve_gateway_path(private_key_path)
    if not resolved_private_key_path.is_file():
        raise FileNotFoundError(f"SSH 私钥不存在: {resolved_private_key_path}")
    normalized_remote_workspace_path = remote_workspace_path.strip()
    if not normalized_remote_workspace_path:
        raise ValueError("remote_workspace_path 不能为空")

    local_port = allocate_ssh_tunnel_port()
    backend_url = f"http://127.0.0.1:{local_port}"
    tunnel = start_ssh_tunnel_process(
        host=host,
        port=port,
        username=username,
        private_key_path=resolved_private_key_path,
        local_port=local_port,
        remote_backend_host=remote_backend_host,
        remote_backend_port=remote_backend_port,
        log_dir=_gateway_root() / "logs",
    )
    try:
        await wait_for_http_ok(f"{backend_url}/api/v1/health", tunnel.process)
    except Exception:
        tunnel.close()
        raise

    workspace_id = GatewayWorkspaceRegistry.build_ssh_workspace_id(
        root_path=normalized_remote_workspace_path,
        host=host,
        port=port,
        username=username,
        remote_backend_host=remote_backend_host,
        remote_backend_port=remote_backend_port,
    )
    return registry.upsert(
        WorkspaceTarget(
            workspace_id=workspace_id,
            name=name or _workspace_name(normalized_remote_workspace_path, "remote"),
            root_path=normalized_remote_workspace_path,
            backend_url=backend_url,
            connection_kind="ssh",
            managed=True,
            remote={
                "host": host,
                "port": port,
                "username": username,
                "remote_backend_host": remote_backend_host,
                "remote_backend_port": remote_backend_port,
            },
        ),
        process=tunnel,
        activate=activate,
    )


async def _create_registry() -> GatewayWorkspaceRegistry:
    registry = GatewayWorkspaceRegistry(storage_path=_gateway_root() / "workspaces.json")
    default_root_path = _default_workspace_root()
    default_backend_url = os.environ.get("BOXTEAM_DEFAULT_BACKEND_URL")
    default_workspace_id: str | None = None
    if default_root_path and default_backend_url:
        root_path = str(default_root_path)
        backend_url = default_backend_url.rstrip("/")
        workspace_id = GatewayWorkspaceRegistry.build_workspace_id("local", root_path, backend_url)
        default_workspace_id = workspace_id
        registry.upsert(
            WorkspaceTarget(
                workspace_id=workspace_id,
                name=os.environ.get("BOXTEAM_DEFAULT_WORKSPACE_NAME") or _workspace_name(root_path, "boxteam_workspace"),
                root_path=root_path,
                backend_url=backend_url,
                connection_kind="local",
                managed=False,
                removable=False,
                system_default=True,
            ),
            activate=True,
        )
        registry.remove_backend_aliases(
            backend_url=backend_url,
            keep_workspace_id=workspace_id,
        )
    gateway_config = load_gateway_config(_gateway_config_workspace_root(default_root_path))
    for configured_workspace in gateway_config.workspaces:
        await _register_ssh_workspace(
            registry=registry,
            name=configured_workspace.name,
            host=configured_workspace.host,
            port=configured_workspace.port,
            username=configured_workspace.username,
            private_key_path=configured_workspace.private_key_path,
            remote_backend_host=configured_workspace.remote_backend_host,
            remote_backend_port=configured_workspace.remote_backend_port,
            remote_workspace_path=configured_workspace.remote_workspace_path,
            activate=configured_workspace.activate,
        )
    if default_workspace_id:
        registry.activate(default_workspace_id)
    registry.ensure_default_workspace_first()
    return registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_project_env()
    registry = await _create_registry()
    app.state.registry = registry
    app.state.http_client = httpx.AsyncClient(timeout=None)
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


def get_http_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http_client", None)
    if not isinstance(client, httpx.AsyncClient):
        raise RuntimeError("Gateway HTTP client 尚未初始化")
    return client


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


@app.get("/api/gateway/local-directories", response_model=APIResponse[LocalDirectoryListDTO])
async def list_local_directories(
    path: str | None = Query(default=None, description="要浏览的本机目录；为空时使用用户主目录"),
    limit: int = Query(default=120, ge=1, le=500),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    root_path = _resolve_local_directory(path)
    entries: list[LocalDirectoryEntryDTO] = []
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
            LocalDirectoryEntryDTO(
                name=entry.name,
                path=str(entry_path),
            )
        )
    parent_path = root_path.parent if root_path.parent != root_path else None
    return APIResponse(
        data=LocalDirectoryListDTO(
            path=str(root_path),
            parent_path=str(parent_path) if parent_path is not None else None,
            home_path=str(Path.home().resolve()),
            entries=entries,
            truncated=len(directories) > limit,
            limit=limit,
        ),
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
    managed_process = None
    if backend_url is None:
        port = allocate_local_port()
        project_root = get_project_root()
        backend_url = f"http://127.0.0.1:{port}"
        managed_process = start_local_backend_process(
            project_root=project_root,
            workspace_root=workspace_root,
            port=port,
            log_dir=_gateway_root() / "logs",
        )
        try:
            await wait_for_http_ok(f"{backend_url}/api/v1/health", managed_process.process)
        except Exception:
            managed_process.close()
            raise
    else:
        await wait_for_http_ok(f"{backend_url}/api/v1/health")

    workspace_id = GatewayWorkspaceRegistry.build_workspace_id(
        "local",
        str(workspace_root),
        backend_url,
    )
    registry.upsert(
        WorkspaceTarget(
            workspace_id=workspace_id,
            name=payload.name or _workspace_name(str(workspace_root)),
            root_path=str(workspace_root),
            backend_url=backend_url,
            connection_kind="local",
            managed=managed_process is not None,
        ),
        process=managed_process,
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
        await _register_ssh_workspace(
            registry=registry,
            name=payload.name,
            host=payload.host,
            port=payload.port,
            username=payload.username,
            private_key_path=payload.private_key_path,
            remote_backend_host=payload.remote_backend_host,
            remote_backend_port=payload.remote_backend_port,
            remote_workspace_path=payload.remote_workspace_path,
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


def _proxy_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP_HEADERS or lower == "host":
            continue
        headers[key] = value
    headers["X-Local-Token"] = LOCAL_TOKEN
    headers["X-Request-ID"] = get_request_id(request)
    return headers


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


async def _stream_proxy_response(response: httpx.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_bytes():
        yield chunk
    await response.aclose()


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy_workspace_api(
    path: str,
    request: Request,
    _: str = Depends(verify_gateway_token),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
    client: httpx.AsyncClient = Depends(get_http_client),
):
    workspace_id = request.headers.get("X-BoxTeam-Workspace-Id")
    try:
        target = registry.resolve(workspace_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    target_url = f"{target.backend_url.rstrip('/')}/api/v1/{path}"
    body = await request.body()
    forwarded = client.build_request(
        request.method,
        target_url,
        params=request.query_params,
        content=body,
        headers=_proxy_headers(request),
    )
    try:
        response = await client.send(forwarded, stream=True)
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail=f"无法连接工作区后端 {target.backend_url}: {error}",
        ) from error
    media_type = response.headers.get("content-type")
    if media_type and "text/event-stream" in media_type:
        return StreamingResponse(
            _stream_proxy_response(response),
            status_code=response.status_code,
            media_type=media_type,
            headers=_response_headers(response),
        )

    content = await response.aread()
    headers = _response_headers(response)
    await response.aclose()
    return Response(
        content=content,
        status_code=response.status_code,
        headers=headers,
        media_type=media_type,
    )
