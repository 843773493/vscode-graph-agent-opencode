"""Backend 复用原生 Gateway 管理组件的具名本机控制入口。"""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import JsonValue
from starlette.requests import HTTPConnection
from starlette.responses import Response

from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.runtime.extension_download import (
    DOWNLOAD_ERROR_HEADER,
    ExtensionDownloadRequest,
    extension_download,
)
from app.gateway.runtime.extension_management import GatewayExtensionManagement
from app.schemas.gateway.extension_host import BackendId
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


def _verify_local_control(request: Request) -> None:
    if (request.client is None or request.client.host not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        or "origin" in request.headers or "sec-fetch-site" in request.headers):
        raise HTTPException(403, "扩展管理组件只接受本机 Backend 控制通道")


def _download_base_url(request: Request) -> str:
    server = request.scope.get("server")
    if server is None:
        raise RuntimeError("扩展下载缺少当前 Gateway 的实际 HTTP listener")
    # 使用实际监听端口，不能信任 Host 或冻结旧代 Gateway 的环境地址。
    host = "[::1]" if ":" in server[0] else "127.0.0.1"
    return f"http://{host}:{server[1]}"


def management_owner(connection: HTTPConnection, backend_id: str | None = None) -> GatewayExtensionManagement:
    owner = connection.app.state.extension_management
    if owner is None:
        raise HTTPException(503, "此原生 Gateway 未配置扩展管理组件")
    if not isinstance(owner, GatewayExtensionManagement):
        raise TypeError("Gateway 扩展管理组件尚未初始化")
    if backend_id is not None and backend_id != owner.backend_id:
        raise ValueError("扩展管理目标不属于原生 Gateway 的安装域")
    owner.assert_healthy()
    return owner


@router.post("/api/gateway/internal/extension-management/{backend_id}", include_in_schema=False,
    dependencies=[Depends(verify_gateway_token)])
async def extension_management_request(backend_id: BackendId, command: dict[str, JsonValue], request: Request,
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[dict[str, JsonValue]]:
    _verify_local_control(request)
    if command.get("method") not in (
        "inventory", "environment", "install", "uninstall", "extension-file-stat", "extension-file-read", "web-publication",
    ):
        raise HTTPException(422, "未登记的扩展安装管理操作")
    try:
        owner = management_owner(request, backend_id)
        if command.get("method") == "install":
            result = await owner.request(command, download_base_url=_download_base_url(request))
        else:
            result = await owner.request(command)
        return APIResponse(data=result, request_id=request_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/api/gateway/internal/extension-management/{backend_id}/downloads/{capability}",
    include_in_schema=False, dependencies=[Depends(verify_gateway_token)], response_model=None)
async def download_extension_asset(backend_id: BackendId, capability: str, payload: ExtensionDownloadRequest,
    request: Request, request_id: Annotated[str, Depends(get_request_id)],
) -> Response:
    _verify_local_control(request)
    owner = request.app.state.extension_management
    if not isinstance(owner, GatewayExtensionManagement) or owner.backend_id != backend_id:
        raise HTTPException(403, "扩展下载不属于本机安装域")
    try:
        # 不调用 assert_healthy：原回执未知时，已受理的安装仍有权结清其下载。
        owner.authorize_download(capability, request_id)
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    try:
        return await extension_download(payload)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except httpx.HTTPError as error:
        # URL可能含临时签名或凭据；只返回传输阶段与类别，不复制异常全文。
        return Response(f"目标扩展下载传输失败: {type(error).__name__}", status_code=502,
            headers={DOWNLOAD_ERROR_HEADER: type(error).__name__}, media_type="text/plain")
