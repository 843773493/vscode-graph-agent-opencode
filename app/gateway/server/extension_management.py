"""Backend 复用原生 Gateway 管理组件的具名本机控制入口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import JsonValue
from starlette.requests import HTTPConnection

from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.runtime.extension_management import GatewayExtensionManagement
from app.schemas.gateway.extension_host import BackendId
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


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
    if (request.client is None or request.client.host not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        or "origin" in request.headers or "sec-fetch-site" in request.headers):
        raise HTTPException(403, "扩展管理组件只接受本机 Backend 控制通道")
    if command.get("method") not in (
        "inventory", "environment", "install", "uninstall", "extension-file-stat", "extension-file-read",
    ):
        raise HTTPException(422, "未登记的扩展安装管理操作")
    try:
        owner = management_owner(request, backend_id)
        return APIResponse(data=await owner.request(command), request_id=request_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
