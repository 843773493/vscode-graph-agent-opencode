from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.core.trace_middleware import get_request_id
from app.gateway.auth import LOCAL_TOKEN, verify_gateway_token
from app.gateway.registry import GatewayWorkspaceRegistry


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


def _proxy_headers(request: Request) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
    }
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


@router.api_route(
    "/api/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_workspace_api(
    path: str,
    request: Request,
    _: str = Depends(verify_gateway_token),
):
    registry = _registry(request)
    workspace_id = request.headers.get("X-BoxTeam-Workspace-Id")
    try:
        target = registry.resolve(workspace_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    target_url = f"{target.backend_url.rstrip('/')}/api/v1/{path}"
    client = _http_client(request)
    forwarded = client.build_request(
        request.method,
        target_url,
        params=request.query_params,
        content=await request.body(),
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
