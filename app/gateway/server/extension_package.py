"""包资源只读代理；当前成员与冻结实例资格由目标 Backend 核验。"""

from __future__ import annotations

import mimetypes
from base64 import b64decode
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import ValidationError

from app.core.resource_uri import extension_package_uri, parse_resource_uri
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
from app.gateway.server.extension_management import management_owner
from app.gateway.server.workspace_proxy import (
    _http_client,
    verify_user_access_for_proxy,
)
from app.schemas.canonical_params import CanonicalGatewayId
from app.schemas.gateway.extension_host import BackendId
from app.schemas.gateway.extension_installation import NativeExtensionFileRequest
from app.schemas.gateway.extension_package import (
    ExtensionPackageFileRequest,
    ExtensionPackageReadDTO,
    ExtensionPackageStatDTO,
)
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
    NativeFileRead,
    NativeFileStat,
)

router = APIRouter()
_CONTENT_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
}


def _content_access(request: Request, x_local_token: str | None = Header(default=None),
                    x_boxteam_federation_token: str | None = Header(default=None),
) -> tuple[GatewayAuthContext, UserAccessContext | None]:
    if x_local_token is not None or x_boxteam_federation_token is not None:
        auth = verify_gateway_access(x_local_token, x_boxteam_federation_token)
    else:
        # 脚本、样式和字体不能附加管理头；同源浏览器沿原 HttpOnly 用户租约，
        # 随后仍须匹配该用户已经取得的实例代际。凭据不进入资源 URL。
        if request.headers.get("sec-fetch-site") != "same-origin":
            raise HTTPException(403, "扩展包资源只接受同源浏览器或 Gateway 访问凭据")
        auth = GatewayAuthContext(kind="local")
    return auth, verify_user_access_for_proxy(request, auth)


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
            if not route.remote and payload.generation is None:
                result = await management_owner(request, payload.backend_id).call({"method": f"extension-file-{operation}",
                    "profile_id": profile_id, "extension_id": native.extension_id, "extension_key": payload.extension_key,
                    "relative_segments": list(segments), "current_profile_member": True},
                    response_type=NativeFileStat if operation == "stat" else NativeFileRead)
                check_access()
                if isinstance(result, NativeFileStat):
                    return APIResponse(data=ExtensionPackageStatDTO(resource_uri=payload.resource_uri,
                        extension_key=payload.extension_key, **result.model_dump()), request_id=request_id)
                return APIResponse(data=ExtensionPackageReadDTO(resource_uri=payload.resource_uri,
                    extension_key=payload.extension_key, content_base64=result.content_base64), request_id=request_id)
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
    except ExtensionManagementError as error:
        status = {"EntryNotFound": 404, "NoPermissions": 403, "Invalid": 409,
                  "InstalledExtensionNotFound": 409, "EntryTooLarge": 413}.get(error.code, 400)
        raise HTTPException(status, detail={"owner": "extension_package", "code": error.code, "message": str(error)}) from error
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


@router.get("/api/gateway/extension-packages/content/{gateway_id}/{backend_id}/{profile_id}/{view_id}/{generation}/{extension_key}/{extension_id}/{relative_path:path}",
    response_class=Response, responses=_CONTENT_RESPONSES)
async def extension_package_content(
    gateway_id: CanonicalGatewayId, backend_id: BackendId, profile_id: str, view_id: str,
    generation: str, extension_key: str, extension_id: str, relative_path: str, request: Request,
    access: Annotated[tuple[GatewayAuthContext, UserAccessContext | None], Depends(_content_access)],
) -> Response:
    return await _package_content(gateway_id, backend_id, profile_id, view_id, generation, extension_key,
        extension_id, relative_path, request, access, gateway_connection_id=None)


@router.get("/api/gateway/extension-packages/via/{gateway_connection_id}/content/{gateway_id}/{backend_id}/{profile_id}/{view_id}/{generation}/{extension_key}/{extension_id}/{relative_path:path}",
    response_class=Response, responses=_CONTENT_RESPONSES)
async def remote_extension_package_content(
    gateway_connection_id: str, gateway_id: CanonicalGatewayId, backend_id: BackendId,
    profile_id: str, view_id: str, generation: str, extension_key: str, extension_id: str,
    relative_path: str, request: Request,
    access: Annotated[tuple[GatewayAuthContext, UserAccessContext | None], Depends(_content_access)],
) -> Response:
    return await _package_content(gateway_id, backend_id, profile_id, view_id, generation, extension_key,
        extension_id, relative_path, request, access, gateway_connection_id=gateway_connection_id)


async def _package_content(
    gateway_id: str, backend_id: str, profile_id: str, view_id: str, generation: str,
    extension_key: str, extension_id: str, relative_path: str, request: Request,
    access: tuple[GatewayAuthContext, UserAccessContext | None], *, gateway_connection_id: str | None,
) -> Response:
    # 路径保留原实例与精确发布选择，让相对模块、样式和字体沿用同一资格。
    # 这只是原资源 URI 的 HTTP 传输地址，不能独立授予运行或文件访问权限。
    if "gateway_connection_id" in request.query_params:
        raise HTTPException(422, "扩展包连接必须通过路径指定，确保相对资源沿用同一连接")
    try:
        payload = ExtensionPackageFileRequest(protocol_version=1, gateway_id=gateway_id, backend_id=backend_id,
            resource_uri=extension_package_uri(gateway_id=gateway_id, backend_id=backend_id,
                profile_id=profile_id, extension_id=extension_id, relative_segments=relative_path.split("/")),
            view_id=view_id, generation=generation, extension_key=extension_key,
            gateway_connection_id=gateway_connection_id)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    result = await _file_request(payload, request, *access, operation="read")
    if isinstance(result, Response):
        return result
    if not isinstance(result.data, ExtensionPackageReadDTO):
        raise HTTPException(502, "目标扩展包没有返回读取正文")
    try:
        content = b64decode(result.data.content_base64, validate=True)
    except ValueError as error:
        raise HTTPException(502, "目标扩展包正文编码无效") from error
    media_type = mimetypes.guess_type(relative_path)[0] or "application/octet-stream"
    return Response(content, media_type=media_type, headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
    })
