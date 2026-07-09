from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.core.env import get_project_root, load_project_env
from app.gateway.processes import (
    allocate_local_port,
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
    return get_project_root() / ".boxteam" / "gateway"


def _workspace_name(root_path: str, fallback: str = "workspace") -> str:
    name = Path(root_path).name
    return name or fallback


async def _create_registry() -> GatewayWorkspaceRegistry:
    registry = GatewayWorkspaceRegistry(storage_path=_gateway_root() / "workspaces.json")
    default_root = os.environ.get("BOXTEAM_DEFAULT_WORKSPACE_ROOT") or os.environ.get("WORKSPACE_ROOT")
    default_backend_url = os.environ.get("BOXTEAM_DEFAULT_BACKEND_URL")
    if default_root and default_backend_url:
        root_path = str(Path(default_root).expanduser().resolve())
        backend_url = default_backend_url.rstrip("/")
        workspace_id = GatewayWorkspaceRegistry.build_workspace_id("local", root_path, backend_url)
        force_default_active = os.environ.get("BOXTEAM_GATEWAY_FORCE_DEFAULT_ACTIVE") == "1"
        registry.upsert(
            WorkspaceTarget(
                workspace_id=workspace_id,
                name=os.environ.get("BOXTEAM_DEFAULT_WORKSPACE_NAME") or _workspace_name(root_path),
                root_path=root_path,
                backend_url=backend_url,
                connection_kind="local",
                managed=False,
            ),
            activate=force_default_active or registry.active_workspace_id is None,
        )
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
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    return APIResponse(
        data=GatewayHealthDTO(active_workspace_id=registry.active_workspace_id)
    )


@app.get("/api/gateway/workspaces", response_model=APIResponse[GatewayWorkspaceListDTO])
async def list_workspaces(
    _: str = Depends(verify_gateway_token),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        )
    )


@app.post("/api/gateway/workspaces/local", response_model=APIResponse[GatewayWorkspaceListDTO])
async def add_local_workspace(
    payload: AddLocalWorkspaceRequest,
    _: str = Depends(verify_gateway_token),
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
    )
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        )
    )


@app.post("/api/gateway/workspaces/ssh", response_model=APIResponse[GatewayWorkspaceListDTO])
async def add_ssh_workspace(
    payload: AddSshWorkspaceRequest,
    _: str = Depends(verify_gateway_token),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    private_key_path = Path(payload.private_key_path).expanduser().resolve()
    if not private_key_path.is_file():
        raise HTTPException(status_code=400, detail=f"SSH 私钥不存在: {private_key_path}")
    if not payload.remote_workspace_path.strip():
        raise HTTPException(status_code=400, detail="remote_workspace_path 不能为空")

    local_port = allocate_local_port()
    backend_url = f"http://127.0.0.1:{local_port}"
    tunnel = start_ssh_tunnel_process(
        host=payload.host,
        port=payload.port,
        username=payload.username,
        private_key_path=private_key_path,
        local_port=local_port,
        remote_backend_host=payload.remote_backend_host,
        remote_backend_port=payload.remote_backend_port,
        log_dir=_gateway_root() / "logs",
    )
    try:
        await wait_for_http_ok(f"{backend_url}/api/v1/health", tunnel.process)
    except Exception:
        tunnel.close()
        raise

    workspace_id = GatewayWorkspaceRegistry.build_workspace_id(
        "ssh",
        payload.remote_workspace_path,
        backend_url,
    )
    registry.upsert(
        WorkspaceTarget(
            workspace_id=workspace_id,
            name=payload.name or _workspace_name(payload.remote_workspace_path, "remote"),
            root_path=payload.remote_workspace_path,
            backend_url=backend_url,
            connection_kind="ssh",
            managed=True,
            remote={
                "host": payload.host,
                "port": payload.port,
                "username": payload.username,
                "remote_backend_host": payload.remote_backend_host,
                "remote_backend_port": payload.remote_backend_port,
            },
        ),
        process=tunnel,
    )
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        )
    )


@app.post(
    "/api/gateway/workspaces/{workspace_id}/activate",
    response_model=APIResponse[ActivateGatewayWorkspaceResultDTO],
)
async def activate_workspace(
    workspace_id: str,
    _: str = Depends(verify_gateway_token),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        registry.activate(workspace_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=ActivateGatewayWorkspaceResultDTO(active_workspace_id=workspace_id)
    )


@app.delete("/api/gateway/workspaces/{workspace_id}", response_model=APIResponse[GatewayWorkspaceListDTO])
async def remove_workspace(
    workspace_id: str,
    _: str = Depends(verify_gateway_token),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    try:
        registry.remove(workspace_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(
        data=GatewayWorkspaceListDTO(
            active_workspace_id=registry.active_workspace_id,
            items=await registry.list_dtos(),
        )
    )


def _proxy_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP_HEADERS or lower == "host":
            continue
        headers[key] = value
    headers["X-Local-Token"] = LOCAL_TOKEN
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
