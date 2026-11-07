"""原生配置文件的认证管理转送；不解析或读写目标文件正文。"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeVar

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.api.sse_heartbeat import SSE_NO_CACHE_HEADERS
from app.core.trace_middleware import get_request_id
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.control.user_access import UserAccessContext
from app.gateway.federation.policy import require_federation_capability
from app.gateway.proxy_upstream import (
    UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS,
    UpstreamResponseBody,
)
from app.gateway.server.extension_host import (
    CLIENT_HEADER,
    _check,
    _installation_route,
    _users,
    extension_client_identity,
)
from app.gateway.server.management_routes import (
    GatewayManagementRoute,
    check_management_route,
)
from app.gateway.server.workspace_proxy import (
    _http_client,
    verify_user_access_for_proxy,
)
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationEventDTO,
    ExtensionConfigurationFileRequest,
    ExtensionConfigurationReadDTO,
    ExtensionConfigurationStatDTO,
    ExtensionConfigurationViewRequest,
    ExtensionConfigurationWriteRequest,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.sse import sse_responses

router = APIRouter()
ConfigurationResult = TypeVar("ConfigurationResult", ExtensionConfigurationReadDTO, ExtensionConfigurationStatDTO)


def _require_access(request: Request, auth: GatewayAuthContext, access: UserAccessContext | None,
                    route: GatewayManagementRoute, *, write: bool = False) -> None:
    check_management_route(request, route)
    _check(request, auth, access, route.leases, capability="extension_host_manage")
    require_federation_capability(request, auth, "configuration_read")
    if write:
        require_federation_capability(request, auth, "configuration_write")
    if access is not None:
        expires = _users(request).expires_at(access)
        if expires is None or datetime.fromisoformat(expires) <= datetime.now(UTC):
            raise PermissionError("配置请求的用户访问租约已过期")


async def _file_request(payload: ExtensionConfigurationFileRequest, request: Request,
    auth: GatewayAuthContext, access: UserAccessContext | None, *, operation: Literal["stat", "read", "write"],
    response_type: type[APIResponse[ConfigurationResult]],
) -> APIResponse[ConfigurationResult] | Response:
    client_id = extension_client_identity(request, auth, access)
    request_id = get_request_id(request)
    try:
        with _installation_route(request, payload, auth) as route:
            _require_access(request, auth, access, route, write=operation == "write")
            prefix = "api/gateway" if route.remote else "api/internal"
            response = await _http_client(request).post(f"{route.base_url.rstrip('/')}/{prefix}/extension-configurations/{operation}",
                headers={**route.headers, CLIENT_HEADER: client_id}, json=payload.model_dump(exclude={"gateway_connection_id"}), timeout=30)
            _require_access(request, auth, access, route, write=operation == "write")
            if response.headers.get("X-Request-ID") != request_id:
                raise ValueError("配置响应的 request_id 与原请求不一致")
            if not response.is_success:
                failure = response.json()
                if not isinstance(failure, dict) or failure.get("request_id") != request_id:
                    raise ValueError("配置错误响应不属于原请求")
                return Response(response.content, status_code=response.status_code, media_type="application/json")
            result = response_type.model_validate_json(response.content)
            if result.code != 0 or result.request_id != request_id or result.data is None or result.data.resource_uri != payload.resource_uri:
                raise ValueError("配置响应不属于原具名资源或请求")
            return result
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (httpx.RequestError, ValidationError) as error:
        raise HTTPException(502, "原生配置操作回执未确认；未重放请求") from error
    except (LookupError, ValueError) as error:
        raise HTTPException(409, str(error)) from error


@router.post("/api/gateway/extension-configurations/stat", response_model=APIResponse[ExtensionConfigurationStatDTO])
async def configuration_stat(payload: ExtensionConfigurationFileRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> APIResponse[ExtensionConfigurationStatDTO] | Response:
    return await _file_request(payload, request, auth, access, operation="stat", response_type=APIResponse[ExtensionConfigurationStatDTO])


@router.post("/api/gateway/extension-configurations/read", response_model=APIResponse[ExtensionConfigurationReadDTO])
async def configuration_read(payload: ExtensionConfigurationFileRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> APIResponse[ExtensionConfigurationReadDTO] | Response:
    return await _file_request(payload, request, auth, access, operation="read", response_type=APIResponse[ExtensionConfigurationReadDTO])


@router.post("/api/gateway/extension-configurations/write", response_model=APIResponse[ExtensionConfigurationStatDTO])
async def configuration_write(payload: ExtensionConfigurationWriteRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> APIResponse[ExtensionConfigurationStatDTO] | Response:
    return await _file_request(payload, request, auth, access, operation="write", response_type=APIResponse[ExtensionConfigurationStatDTO])


class _ConfigurationStreamResponse(StreamingResponse):
    def __init__(self, body: UpstreamResponseBody, request: Request, auth: GatewayAuthContext,
                 access: UserAccessContext | None, route: GatewayManagementRoute) -> None:
        self._body = body

        async def generate():
            try:
                async for chunk in body:
                    try:
                        _require_access(request, auth, access, route)
                    except (HTTPException, PermissionError, LookupError):
                        event = ExtensionConfigurationEventDTO(changes=[], overflow=False, error="配置读取权限、用户租约或目标路由已撤销")
                        yield f"\n\nevent: error\ndata: {event.model_dump_json()}\n\n".encode()
                        return
                    yield chunk
            finally:
                await body.aclose()

        super().__init__(generate(), media_type="text/event-stream", headers=SSE_NO_CACHE_HEADERS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._body.aclose()


@router.post("/api/gateway/extension-configurations/events", response_class=StreamingResponse,
    responses=sse_responses("精确目标配置变更", {name: ExtensionConfigurationEventDTO for name in ("ready", "changes", "error")},
                            models={"ExtensionConfigurationEventDTO": ExtensionConfigurationEventDTO}))
async def configuration_events(payload: ExtensionConfigurationViewRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> Response:
    client_id = extension_client_identity(request, auth, access)
    references = ExitStack()
    response: httpx.Response | None = None
    handed_off = False
    try:
        route = references.enter_context(_installation_route(request, payload, auth, streaming=True))
        _require_access(request, auth, access, route)
        prefix = "api/gateway" if route.remote else "api/internal"
        client = _http_client(request, streaming=True)
        upstream = client.build_request("POST", f"{route.base_url.rstrip('/')}/{prefix}/extension-configurations/events",
            headers={**route.headers, CLIENT_HEADER: client_id}, json=payload.model_dump(exclude={"gateway_connection_id"}), timeout=None)
        async with asyncio.timeout(UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS):
            response = await client.send(upstream, stream=True)
        _require_access(request, auth, access, route)
        if response.headers.get("X-Request-ID") != get_request_id(request):
            raise ValueError("配置事件流返回了不同 request_id")
        if not response.is_success:
            await response.aread()
            failure = response.json()
            if not isinstance(failure, dict) or failure.get("request_id") != get_request_id(request):
                raise ValueError("配置事件流错误回执不属于原请求")
            return Response(response.content, status_code=response.status_code, media_type="application/json")
        if response.headers.get("content-type", "").split(";", 1)[0].strip() != "text/event-stream":
            raise ValueError("配置事件流返回了错误媒体类型")
        result = _ConfigurationStreamResponse(UpstreamResponseBody(response, on_close=references.close), request, auth, access, route)
        handed_off = True
        return result
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (LookupError, ValueError) as error:
        raise HTTPException(409, str(error)) from error
    except (httpx.RequestError, TimeoutError) as error:
        raise HTTPException(502, "原生配置事件流连接失败") from error
    finally:
        if not handed_off:
            if response is not None:
                await UpstreamResponseBody(response, on_close=references.close).aclose()
            else:
                references.close()
