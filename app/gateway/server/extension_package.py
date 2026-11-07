"""包资源只读代理；当前成员与冻结实例资格由目标 Backend 核验。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError

from app.core.resource_uri import parse_resource_uri
from app.core.trace_middleware import get_request_id
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.control.user_access import UserAccessContext
from app.gateway.server.extension_host import (
    CLIENT_HEADER,
    _check,
    _check_access,
    _check_inventory_permission,
    _check_management_response,
    _check_management_route,
    _installation_route,
    _users,
    extension_client_identity,
)
from app.gateway.server.workspace_proxy import (
    _http_client,
    verify_user_access_for_proxy,
)
from app.schemas.gateway.extension_installation import NativeExtensionFileRequest
from app.schemas.gateway.extension_package import (
    ExtensionPackageFileRequest,
    ExtensionPackageReadDTO,
    ExtensionPackageStatDTO,
)
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.extension_host.management import (
    NativeFileRead,
    NativeFileStat,
)

router = APIRouter()


async def _file_request(payload: ExtensionPackageFileRequest, request: Request,
                        auth: GatewayAuthContext, access: UserAccessContext | None, *,
                        operation: Literal["stat", "read"]) -> APIResponse[ExtensionPackageStatDTO] | APIResponse[ExtensionPackageReadDTO] | Response:
    request_id = get_request_id(request)
    client_id = extension_client_identity(request, auth, access)
    try:
        with _installation_route(request, payload, auth) as route:
            def check_access() -> None:
                _check_access(access, route.leases)
                _check_management_route(request, route)
                if payload.generation is None:
                    _check_inventory_permission(request, auth)
                else:
                    _check(request, auth, access, route.leases, capability="extension_host_manage")
                if access is not None:
                    expires = _users(request).expires_at(access)
                    if expires is None or datetime.fromisoformat(expires) <= datetime.now(UTC):
                        raise PermissionError("扩展包请求的用户访问租约已过期")

            check_access()
            resource = parse_resource_uri(payload.resource_uri)
            backend_id, profile_id, publisher, name, *segments = resource.tail_segments
            native = NativeExtensionFileRequest(protocol_version=payload.protocol_version, gateway_id=payload.gateway_id,
                backend_id=backend_id, profile_id=profile_id, extension_id=f"{publisher}.{name}", extension_key=payload.extension_key,
                relative_segments=segments, current_profile_member=payload.generation is None,
                generation=payload.generation, view_id=payload.view_id)
            prefix = "api/gateway/extension-packages" if route.remote else "api/internal/extension-files"
            response = await _http_client(request).post(f"{route.base_url.rstrip('/')}/{prefix}/{operation}",
                headers={**route.headers, CLIENT_HEADER: client_id},
                json=payload.model_dump(exclude={"gateway_connection_id"}) if route.remote else native.model_dump(), timeout=30)
            check_access()
            _check_management_response(response, request_id)
            if not response.is_success:
                return Response(response.content, status_code=response.status_code, media_type="application/json", headers={"Cache-Control": "no-store"})
            if route.remote:
                if operation == "stat":
                    envelope = APIResponse[ExtensionPackageStatDTO].model_validate_json(response.content)
                else:
                    envelope = APIResponse[ExtensionPackageReadDTO].model_validate_json(response.content)
                if (envelope.code != 0 or envelope.data is None or envelope.data.resource_uri != payload.resource_uri
                    or envelope.data.extension_key != payload.extension_key):
                    raise ValueError("扩展包响应不属于原 URI 或精确发布键")
                return envelope
            if operation == "stat":
                result = APIResponse[NativeFileStat].model_validate_json(response.content)
                if result.code != 0 or result.data is None:
                    raise ValueError("目标没有返回扩展包 stat")
                return APIResponse(data=ExtensionPackageStatDTO(resource_uri=payload.resource_uri,
                    extension_key=payload.extension_key, **result.data.model_dump()), request_id=request_id)
            read_result = APIResponse[NativeFileRead].model_validate_json(response.content)
            if read_result.code != 0 or read_result.data is None:
                raise ValueError("目标没有返回扩展包正文")
            return APIResponse(data=ExtensionPackageReadDTO(resource_uri=payload.resource_uri,
                extension_key=payload.extension_key, content_base64=read_result.data.content_base64), request_id=request_id)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except ValidationError as error:
        raise HTTPException(502, "目标扩展包响应结构无效") from error
    except (LookupError, ValueError, RuntimeError) as error:
        raise HTTPException(409, str(error)) from error
    except httpx.RequestError as error:
        raise HTTPException(502, "目标扩展包读取失败") from error


@router.post("/api/gateway/extension-packages/stat", response_model=APIResponse[ExtensionPackageStatDTO])
async def extension_package_stat(payload: ExtensionPackageFileRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> APIResponse[ExtensionPackageStatDTO] | APIResponse[ExtensionPackageReadDTO] | Response:
    return await _file_request(payload, request, auth, access, operation="stat")


@router.post("/api/gateway/extension-packages/read", response_model=APIResponse[ExtensionPackageReadDTO])
async def extension_package_read(payload: ExtensionPackageFileRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    access: Annotated[UserAccessContext | None, Depends(verify_user_access_for_proxy)],
) -> APIResponse[ExtensionPackageStatDTO] | APIResponse[ExtensionPackageReadDTO] | Response:
    return await _file_request(payload, request, auth, access, operation="read")
