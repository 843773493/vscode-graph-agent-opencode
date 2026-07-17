from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.gateway.auth import LOCAL_TOKEN, verify_gateway_token
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.service_types import GatewayServiceName


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

SERVICE_PATHS: dict[str, GatewayServiceName] = {
    "terminal-manager": "terminal_manager",
    "browser-manager": "browser_manager",
}


class UpstreamWebSocket(Protocol):
    async def send(self, message: str | bytes) -> None: ...

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


def _registry(app: object) -> GatewayWorkspaceRegistry:
    state = getattr(app, "state", None)
    registry = getattr(state, "registry", None)
    if not isinstance(registry, GatewayWorkspaceRegistry):
        raise RuntimeError("Gateway registry 尚未初始化")
    return registry


def _http_client(app: object) -> httpx.AsyncClient:
    state = getattr(app, "state", None)
    client = getattr(state, "http_client", None)
    if not isinstance(client, httpx.AsyncClient):
        raise RuntimeError("Gateway HTTP client 尚未初始化")
    return client


def _service_name(service_path: str) -> GatewayServiceName:
    service = SERVICE_PATHS.get(service_path)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知辅助服务: {service_path}")
    return service


def _proxy_request_headers(request: Request) -> dict[str, str]:
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
        and key.lower() not in {"host", "x-local-token"}
    }


def _proxy_response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }


async def _stream_response(response: httpx.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_bytes():
        yield chunk
    await response.aclose()


@router.api_route(
    "/api/gateway/workspaces/{workspace_id}/{service_path}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_auxiliary_http(
    workspace_id: str,
    service_path: str,
    path: str,
    request: Request,
    _: str = Depends(verify_gateway_token),
):
    service = _service_name(service_path)
    registry = _registry(request.app)
    try:
        service_url = registry.resolve_service_url(workspace_id, service)
    except (LookupError, ValueError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    target_url = f"{service_url.rstrip('/')}/{path}"
    client = _http_client(request.app)
    forwarded = client.build_request(
        request.method,
        target_url,
        params=request.query_params,
        content=await request.body(),
        headers=_proxy_request_headers(request),
    )
    try:
        response = await client.send(forwarded, stream=True)
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail=(
                f"无法连接工作区辅助服务: workspace_id={workspace_id}, "
                f"service={service}: {error}"
            ),
        ) from error
    media_type = response.headers.get("content-type")
    if media_type and "text/event-stream" in media_type:
        return StreamingResponse(
            _stream_response(response),
            status_code=response.status_code,
            media_type=media_type,
            headers=_proxy_response_headers(response),
        )
    content = await response.aread()
    headers = _proxy_response_headers(response)
    await response.aclose()
    return Response(
        content=content,
        status_code=response.status_code,
        headers=headers,
        media_type=media_type,
    )


def _websocket_target(base_url: str, socket_path: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path.rstrip('/')}/{socket_path}"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


async def _relay_client_to_upstream(
    websocket: WebSocket,
    upstream: UpstreamWebSocket,
) -> None:
    while True:
        message = await websocket.receive()
        message_type = message["type"]
        if message_type == "websocket.disconnect":
            return
        text = message.get("text")
        if text is not None:
            await upstream.send(text)
            continue
        body = message.get("bytes")
        if body is not None:
            await upstream.send(body)


async def _relay_upstream_to_client(
    websocket: WebSocket,
    upstream: UpstreamWebSocket,
) -> None:
    async for message in upstream:
        if isinstance(message, str):
            await websocket.send_text(message)
        else:
            await websocket.send_bytes(message)


async def _proxy_auxiliary_websocket(
    *,
    websocket: WebSocket,
    workspace_id: str,
    service: GatewayServiceName,
    socket_path: str,
) -> None:
    if websocket.query_params.get("token") != LOCAL_TOKEN:
        await websocket.close(code=1008, reason="invalid local token")
        return
    registry = _registry(websocket.app)
    try:
        service_url = registry.resolve_service_url(workspace_id, service)
    except (LookupError, ValueError) as error:
        await websocket.close(code=1011, reason=str(error)[:120])
        return
    await websocket.accept()
    target_url = _websocket_target(service_url, socket_path)
    try:
        async with connect(target_url) as upstream:
            tasks = {
                asyncio.create_task(_relay_client_to_upstream(websocket, upstream)),
                asyncio.create_task(_relay_upstream_to_client(websocket, upstream)),
            }
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
    except (ConnectionClosed, WebSocketDisconnect):
        return
    except Exception as error:
        await websocket.close(code=1011, reason=str(error)[:120])


@router.websocket(
    "/api/gateway/workspaces/{workspace_id}/terminal-manager/terminal"
)
async def proxy_terminal_websocket(websocket: WebSocket, workspace_id: str) -> None:
    await _proxy_auxiliary_websocket(
        websocket=websocket,
        workspace_id=workspace_id,
        service="terminal_manager",
        socket_path="terminal",
    )


@router.websocket(
    "/api/gateway/workspaces/{workspace_id}/browser-manager/browser"
)
async def proxy_browser_websocket(websocket: WebSocket, workspace_id: str) -> None:
    await _proxy_auxiliary_websocket(
        websocket=websocket,
        workspace_id=workspace_id,
        service="browser_manager",
        socket_path="browser",
    )


@router.get("/api/gateway/attach/{kind}/{path:path}")
async def proxy_attach_frontend(kind: str, path: str, request: Request):
    frontend_urls = getattr(request.app.state, "attach_frontend_urls", {})
    frontend_url = frontend_urls.get(kind)
    if not isinstance(frontend_url, str):
        raise HTTPException(status_code=404, detail=f"未知 attach 前端: {kind}")
    target_url = f"{frontend_url.rstrip('/')}/{path}"
    client = _http_client(request.app)
    try:
        response = await client.get(
            target_url,
            params=request.query_params,
            headers=_proxy_request_headers(request),
        )
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail=f"attach 前端不可访问: kind={kind}: {error}",
        ) from error
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=_proxy_response_headers(response),
        media_type=response.headers.get("content-type"),
    )
