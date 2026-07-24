from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.core.trace_middleware import get_request_id
from app.core.path_utils import get_gateway_root
from app.gateway.auth import GatewayAuthContext, LOCAL_TOKEN, verify_gateway_access
from app.gateway.credentials import FederationCredentialStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget


router = APIRouter()

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


def _registry(request: Request) -> GatewayWorkspaceRegistry:
    registry = getattr(request.app.state, "registry", None)
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise RuntimeError("Gateway registry 尚未初始化")
    return registry


def _http_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http_client", None)
    if not isinstance(client, httpx.AsyncClient):
        raise RuntimeError("Gateway HTTP client 尚未初始化")
    return client


def _proxy_headers(
    request: Request,
    target: WorkspaceTarget | None = None,
    *,
    include_credentials: bool = True,
) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
        and key.lower()
        not in {
            "host",
            "x-local-token",
            "x-boxteam-federation-token",
            "x-boxteam-workspace-id",
        }
    }
    headers["X-Request-ID"] = get_request_id(request)
    if target is not None and target.connection_kind == "remote_gateway":
        connection_id = target.remote_gateway_connection_id
        remote_workspace_id = target.remote_workspace_id
        if connection_id is None or remote_workspace_id is None:
            raise RuntimeError("远程投影工作区缺少 Gateway 连接信息")
        headers["X-BoxTeam-Workspace-Id"] = remote_workspace_id
        if include_credentials:
            credential = FederationCredentialStore(
                storage_path=get_gateway_root() / "credentials" / "federation.json"
            ).get(connection_id)
            headers["X-BoxTeam-Federation-Token"] = credential.token
    elif include_credentials:
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


async def _proxy_workspace_request(
    path: str,
    request: Request,
    *,
    auth: GatewayAuthContext | None,
    include_credentials: bool,
) -> Response:
    registry = _registry(request)
    workspace_id = request.headers.get("X-BoxTeam-Workspace-Id")
    try:
        target = registry.resolve(workspace_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if (
        auth is not None
        and auth.kind == "federation"
        and target.connection_kind != "local"
    ):
        raise HTTPException(
            status_code=400,
            detail="bounded federation 禁止通过远程 Gateway 继续代理嵌套工作区",
        )

    target_url = (
        f"{registry.remote_gateway_url(target.remote_gateway_connection_id)}/api/v1/{path}"
        if (
            target.connection_kind == "remote_gateway"
            and target.remote_gateway_connection_id is not None
        )
        else f"{target.backend_url.rstrip('/')}/api/v1/{path}"
    )
    client = _http_client(request)
    forwarded = client.build_request(
        request.method,
        target_url,
        params=request.query_params,
        content=await request.body(),
        headers=_proxy_headers(
            request,
            target,
            include_credentials=include_credentials,
        ),
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


@router.api_route(
    "/api/v1/context/read",
    methods=["POST"],
)
async def proxy_context_read(request: Request) -> Response:
    """按工作区 ID 转发结构化上下文读取，不要求 Gateway 凭据。"""
    return await _proxy_workspace_request(
        "context/read",
        request,
        auth=None,
        include_credentials=False,
    )


@router.api_route(
    "/api/v1/context/search",
    methods=["POST"],
)
async def proxy_context_search(request: Request) -> Response:
    """按工作区 ID 转发结构化上下文搜索，不要求 Gateway 凭据。"""
    return await _proxy_workspace_request(
        "context/search",
        request,
        auth=None,
        include_credentials=False,
    )


@router.api_route(
    "/api/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_workspace_api(
    path: str,
    request: Request,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
) -> Response:
    return await _proxy_workspace_request(
        path,
        request,
        auth=auth,
        include_credentials=True,
    )
