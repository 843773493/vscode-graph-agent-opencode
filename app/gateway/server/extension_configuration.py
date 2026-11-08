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
from app.gateway.federation.policy import (
    federation_capability_allowed,
    require_federation_capability,
)
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
    management_gateway_route,
)
from app.gateway.server.workspace_proxy import (
    _http_client,
    verify_user_access_for_proxy,
)
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationEventDTO,
    ExtensionConfigurationFileRequest,
    ExtensionConfigurationProfileFileRequest,
    ExtensionConfigurationProfileViewRequest,
    ExtensionConfigurationProfileWriteRequest,
    ExtensionConfigurationReadDTO,
    ExtensionConfigurationStatDTO,
    ExtensionConfigurationTargetListDTO,
    ExtensionConfigurationTargetListRequest,
    ExtensionConfigurationTargetRequest,
    ExtensionConfigurationViewDTO,
    ExtensionConfigurationViewRequest,
    ExtensionConfigurationWriteRequest,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.sse import sse_responses

router = APIRouter()
ConfigurationResult = TypeVar("ConfigurationResult", ExtensionConfigurationReadDTO, ExtensionConfigurationStatDTO, ExtensionConfigurationViewDTO)


@router.post("/api/gateway/extension-configurations/targets", response_model=APIResponse[ExtensionConfigurationTargetListDTO])
async def configuration_targets(payload: ExtensionConfigurationTargetListRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> APIResponse[ExtensionConfigurationTargetListDTO] | Response:
    try:
        require_federation_capability(request, auth, "configuration_read")
        if access is not None:
            expires = _users(request).expires_at(access)
            if expires is None or datetime.fromisoformat(expires) <= datetime.now(UTC):
                raise PermissionError("配置目标发现的用户访问租约已过期")
        with management_gateway_route(request, gateway_id=payload.gateway_id,
                gateway_connection_id=payload.gateway_connection_id, auth=auth) as remote:
            request_id = get_request_id(request)
            if remote is not None:
                response = await _http_client(request).post(
                    f"{remote.base_url.rstrip('/')}/api/gateway/extension-configurations/targets",
                    headers=remote.headers,
                    json=payload.model_dump(exclude={"gateway_connection_id"}), timeout=10)
                check_management_route(request, remote)
                require_federation_capability(request, auth, "configuration_read")
                if access is not None:
                    expires = _users(request).expires_at(access)
                    if expires is None or datetime.fromisoformat(expires) <= datetime.now(UTC):
                        raise PermissionError("配置目标发现的用户访问租约已过期")
                if response.headers.get("X-Request-ID") != request_id:
                    raise ValueError("配置目标发现响应的 request_id 与原请求不一致")
                if not response.is_success:
                    failure = response.json()
                    if not isinstance(failure, dict) or failure.get("request_id") != request_id:
                        raise ValueError("配置目标发现错误回执不属于原请求")
                    return Response(response.content, status_code=response.status_code, media_type="application/json")
                result = APIResponse[ExtensionConfigurationTargetListDTO].model_validate_json(response.content)
                if (result.code != 0 or result.request_id != request_id or result.data is None
                    or result.data.gateway_id != payload.gateway_id
                    or result.data.protocol_version != payload.protocol_version):
                    raise ValueError("配置目标发现回执不属于原 Gateway 或请求")
                return result

            registry = request.app.state.registry
            registered_ids = {owner.backend_id for owner in registry.backend_owners()
                if owner.backend_id is not None and owner.error is None
                and owner.process is not None and owner.process.is_alive()}
            registered_ids.update(target.backend_id for target in registry.targets()
                if target.gateway_id == registry.gateway_id and target.connection_kind == "local"
                and target.backend_id is not None and not target.managed
                and target.connection_error is None and target.backend_url)
            backend_ids = []
            for backend_id in sorted(registered_ids):
                try:
                    with registry.backend_management_routes(backend_id=backend_id) as candidates:
                        if (len(candidates) == 1 and candidates[0].backend_id == backend_id
                            and (access is None or not access.invalidated.is_set())):
                            backend_ids.append(backend_id)
                except LookupError:
                    continue
            if access is not None:
                expires = _users(request).expires_at(access)
                if access.invalidated.is_set() or expires is None or datetime.fromisoformat(expires) <= datetime.now(UTC):
                    raise PermissionError("配置目标发现的用户访问租约已失效")
            require_federation_capability(request, auth, "configuration_read")
            return APIResponse(data=ExtensionConfigurationTargetListDTO(
                protocol_version=payload.protocol_version, gateway_id=payload.gateway_id,
                backend_ids=backend_ids), request_id=request_id)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (LookupError, ValueError, RuntimeError) as error:
        raise HTTPException(409, str(error)) from error
    except httpx.RequestError as error:
        raise HTTPException(502, "远端配置目标发现连接失败") from error


def _require_access(request: Request, auth: GatewayAuthContext, access: UserAccessContext | None,
                    route: GatewayManagementRoute, payload: ExtensionConfigurationTargetRequest, *, write: bool = False) -> None:
    check_management_route(request, route)
    _check(request, auth, access, route.leases, capability="configuration_read")
    if isinstance(payload, ExtensionConfigurationViewRequest):
        _check(request, auth, access, route.leases, capability="extension_host_manage")
    if write:
        require_federation_capability(request, auth, "configuration_write")
    if access is not None:
        expires = _users(request).expires_at(access)
        if expires is None or datetime.fromisoformat(expires) <= datetime.now(UTC):
            raise PermissionError("配置请求的用户访问租约已过期")


async def _file_request(payload: ExtensionConfigurationTargetRequest, request: Request,
    auth: GatewayAuthContext, access: UserAccessContext | None, *, operation: Literal["stat", "read", "write", "prepare"],
    response_type: type[APIResponse[ConfigurationResult]],
) -> APIResponse[ConfigurationResult] | Response:
    client_id = extension_client_identity(request, auth, access)
    request_id = get_request_id(request)
    try:
        with _installation_route(request, payload, auth) as route:
            _require_access(request, auth, access, route, payload, write=operation == "write")
            prefix = "api/gateway" if route.remote else "api/internal"
            response = await _http_client(request).post(f"{route.base_url.rstrip('/')}/{prefix}/extension-configurations/{operation}",
                headers={**route.headers, CLIENT_HEADER: client_id}, json=payload.model_dump(exclude={"gateway_connection_id"}, exclude_none=True), timeout=30)
            _require_access(request, auth, access, route, payload, write=operation == "write")
            if response.headers.get("X-Request-ID") != request_id:
                raise ValueError("配置响应的 request_id 与原请求不一致")
            if not response.is_success:
                failure = response.json()
                if not isinstance(failure, dict) or failure.get("request_id") != request_id:
                    raise ValueError("配置错误响应不属于原请求")
                return Response(response.content, status_code=response.status_code, media_type="application/json")
            result = response_type.model_validate_json(response.content)
            if result.code != 0 or result.request_id != request_id or result.data is None:
                raise ValueError("配置响应不属于原具名资源或请求")
            if isinstance(payload, (ExtensionConfigurationFileRequest, ExtensionConfigurationProfileFileRequest)):
                if not isinstance(result.data, (ExtensionConfigurationReadDTO, ExtensionConfigurationStatDTO)) or result.data.resource_uri != payload.resource_uri:
                    raise ValueError("配置响应不属于原具名资源")
            elif (not isinstance(result.data, ExtensionConfigurationViewDTO)
                or result.data.model_dump(include=set(ExtensionConfigurationTargetRequest.model_fields) - {"gateway_connection_id"})
                != payload.model_dump(exclude={"gateway_connection_id"})):
                raise ValueError("配置准备回执不属于原目标视图")
            if isinstance(result.data, ExtensionConfigurationViewDTO):
                # Backend 只提供文件能力；原生 Gateway 在披露时收窄主体授权。
                # Relay 保留目标的拒绝，不能用本机访问权限代授远端写入。
                result = result.model_copy(update={"data": result.data.model_copy(update={
                    "configuration_write_allowed": result.data.configuration_write_allowed
                    and federation_capability_allowed(request, auth, "configuration_write"),
                })})
            return result
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (httpx.RequestError, ValidationError) as error:
        raise HTTPException(502, "原生配置操作回执未确认；未重放请求") from error
    except (LookupError, ValueError) as error:
        raise HTTPException(409, str(error)) from error


@router.post("/api/gateway/extension-configurations/prepare", response_model=APIResponse[ExtensionConfigurationViewDTO])
async def configuration_prepare(payload: ExtensionConfigurationTargetRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> APIResponse[ExtensionConfigurationViewDTO] | Response:
    return await _file_request(payload, request, auth, access, operation="prepare", response_type=APIResponse[ExtensionConfigurationViewDTO])


@router.post("/api/gateway/extension-configurations/stat", response_model=APIResponse[ExtensionConfigurationStatDTO])
async def configuration_stat(payload: ExtensionConfigurationFileRequest | ExtensionConfigurationProfileFileRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> APIResponse[ExtensionConfigurationStatDTO] | Response:
    return await _file_request(payload, request, auth, access, operation="stat", response_type=APIResponse[ExtensionConfigurationStatDTO])


@router.post("/api/gateway/extension-configurations/read", response_model=APIResponse[ExtensionConfigurationReadDTO])
async def configuration_read(payload: ExtensionConfigurationFileRequest | ExtensionConfigurationProfileFileRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> APIResponse[ExtensionConfigurationReadDTO] | Response:
    return await _file_request(payload, request, auth, access, operation="read", response_type=APIResponse[ExtensionConfigurationReadDTO])


@router.post("/api/gateway/extension-configurations/write", response_model=APIResponse[ExtensionConfigurationStatDTO])
async def configuration_write(payload: ExtensionConfigurationWriteRequest | ExtensionConfigurationProfileWriteRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> APIResponse[ExtensionConfigurationStatDTO] | Response:
    return await _file_request(payload, request, auth, access, operation="write", response_type=APIResponse[ExtensionConfigurationStatDTO])


class _ConfigurationStreamResponse(StreamingResponse):
    def __init__(self, body: UpstreamResponseBody, request: Request, auth: GatewayAuthContext,
                 access: UserAccessContext | None, route: GatewayManagementRoute, payload: ExtensionConfigurationViewRequest | ExtensionConfigurationProfileViewRequest) -> None:
        self._body = body

        async def generate():
            try:
                async for chunk in body:
                    try:
                        _require_access(request, auth, access, route, payload)
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
async def configuration_events(payload: ExtensionConfigurationViewRequest | ExtensionConfigurationProfileViewRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> Response:
    client_id = extension_client_identity(request, auth, access)
    references = ExitStack()
    response: httpx.Response | None = None
    handed_off = False
    try:
        route = references.enter_context(_installation_route(request, payload, auth, streaming=True))
        _require_access(request, auth, access, route, payload)
        prefix = "api/gateway" if route.remote else "api/internal"
        client = _http_client(request, streaming=True)
        upstream = client.build_request("POST", f"{route.base_url.rstrip('/')}/{prefix}/extension-configurations/events",
            headers={**route.headers, CLIENT_HEADER: client_id}, json=payload.model_dump(exclude={"gateway_connection_id"}, exclude_none=True), timeout=None)
        async with asyncio.timeout(UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS):
            response = await client.send(upstream, stream=True)
        _require_access(request, auth, access, route, payload)
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
        result = _ConfigurationStreamResponse(UpstreamResponseBody(response, on_close=references.close), request, auth, access, route, payload)
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
