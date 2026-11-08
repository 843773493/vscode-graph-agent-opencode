"""自有包管理及精确插件实例路由；不保存工作区业务事实。"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.federation.policy import require_federation_capability
from app.gateway.proxy_upstream import UpstreamResponseBody, build_upstream_url
from app.gateway.runtime.service_plugins.runtime import (
    GatewayPluginRuntime,
    gateway_plugin_bindings,
)
from app.gateway.server.management_routes import (
    check_management_route,
    management_gateway_route,
)
from app.schemas.gateway.service_plugin import (
    InstalledPluginPackageDTO,
    InstallPluginRequest,
    PluginBinding,
    PluginInventoryResultDTO,
    PluginRuntimeStatusDTO,
    PluginSlotStatusDTO,
    PluginTarget,
    PluginUninstallResultDTO,
    PluginVersionRequest,
    RestartPluginRequest,
)
from app.schemas.internal_v2.common import APIResponse

router = APIRouter()


def plugin_owner(request: Request) -> GatewayPluginRuntime:
    return request.app.state.service_plugins


async def relay(request: Request, target: PluginTarget, auth: GatewayAuthContext) -> httpx.Response | None:
    with management_gateway_route(request, gateway_id=target.gateway_id,
        gateway_connection_id=target.gateway_connection_id, auth=auth) as route:
        if route is None:
            return None
        body = await request.json()
        body.pop("gateway_connection_id", None)
        response = await request.app.state.http_client.post(route.base_url + request.url.path,
            json=body, headers=route.headers)
        check_management_route(request, route)
        return response


@router.post("/api/gateway/service-plugins/inventory")
async def inventory(payload: PluginTarget, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    owner: Annotated[GatewayPluginRuntime, Depends(plugin_owner)],
) -> APIResponse[PluginInventoryResultDTO]:
    response = await relay(request, payload, auth)
    if response is not None:
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail"))
        return APIResponse[PluginInventoryResultDTO].model_validate(response.json())
    require_federation_capability(request, auth, "service_plugin_installation_manage")
    packages = await asyncio.to_thread(owner.packages.inventory)
    require_federation_capability(request, auth, "service_plugin_installation_manage")
    data = PluginInventoryResultDTO.model_validate({"packages": [package.model_dump() for package in packages],
        "sources": {"configured": False, "message": "插件源尚未配置"}})
    return APIResponse[PluginInventoryResultDTO](data=data, request_id=get_request_id(request))


@router.post("/api/gateway/service-plugins/install")
async def install(payload: InstallPluginRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    owner: Annotated[GatewayPluginRuntime, Depends(plugin_owner)],
) -> APIResponse[InstalledPluginPackageDTO]:
    # 原生路径是明确目标机器参数；联邦调用不能借本跳路径安装。
    response = await relay(request, payload, auth)
    if response is not None:
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail"))
        return APIResponse[InstalledPluginPackageDTO].model_validate(response.json())
    require_federation_capability(request, auth, "service_plugin_installation_manage")
    if auth.kind != "local":
        raise HTTPException(422, "远端包上传／自有来源尚未接入，不接受远端原生路径安装")
    path = Path(payload.path)
    if not path.is_absolute():
        raise HTTPException(422, "本地包入口必须是目标机器的明确绝对路径")
    try:
        if payload.development_directory:
            package = await run_sync_shielded(owner.packages.install_directory, path)
        else:
            package = await run_sync_shielded(owner.packages.install_zip, path, payload.expected_digest)
    except (ValueError, FileNotFoundError, zipfile.BadZipFile) as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return APIResponse[InstalledPluginPackageDTO](data=InstalledPluginPackageDTO.model_validate(package.model_dump()),
        request_id=get_request_id(request))


@router.post("/api/gateway/service-plugins/uninstall")
async def uninstall(payload: PluginVersionRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    owner: Annotated[GatewayPluginRuntime, Depends(plugin_owner)],
) -> APIResponse[PluginUninstallResultDTO]:
    response = await relay(request, payload, auth)
    if response is not None:
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail"))
        return APIResponse[PluginUninstallResultDTO].model_validate(response.json())
    require_federation_capability(request, auth, "service_plugin_installation_manage")
    try:
        async with request.app.state.gateway_config_reload.mutation_guard():
            require_federation_capability(request, auth, "service_plugin_installation_manage")
            bindings = gateway_plugin_bindings(request.app.state.gateway_config_reload.config.payload)
            references = owner.references(payload.package_id, payload.package_version)
            references.extend(f"configuration:{binding.plugin_id}" for binding in bindings
                if binding.package_id == payload.package_id and binding.package_version == payload.package_version)
            pending = request.app.state.gateway_state.get_pending_config_candidate(config_domain="gateway")
            if pending is not None and pending.state not in {"active", "discarded", "rejected"}:
                references.extend(f"candidate:{pending.candidate_id}:{binding.plugin_id}"
                    for binding in gateway_plugin_bindings(pending.payload)
                    if binding.package_id == payload.package_id and binding.package_version == payload.package_version)
            await run_sync_shielded(owner.packages.uninstall, payload.package_id, payload.package_version,
                references=references)
    except (ValueError, RuntimeError) as error:
        raise HTTPException(409, str(error)) from error
    data = PluginUninstallResultDTO(package_id=payload.package_id, package_version=payload.package_version,
        uninstalled=True)
    return APIResponse[PluginUninstallResultDTO](data=data, request_id=get_request_id(request))


@router.post("/api/gateway/service-plugins/status")
async def status(payload: PluginTarget, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    owner: Annotated[GatewayPluginRuntime, Depends(plugin_owner)],
) -> APIResponse[PluginRuntimeStatusDTO]:
    response = await relay(request, payload, auth)
    if response is not None:
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail"))
        return APIResponse[PluginRuntimeStatusDTO].model_validate(response.json())
    require_federation_capability(request, auth, "service_plugin_runtime_manage")
    data = PluginRuntimeStatusDTO.model_validate({"plugins": [slot.describe() for slot in owner.slots.values()],
        "configured_bindings": [binding.model_dump(mode="json") for binding in gateway_plugin_bindings(
            request.app.state.gateway_config_reload.config.payload)]})
    return APIResponse[PluginRuntimeStatusDTO](data=data, request_id=get_request_id(request))


@router.post("/api/gateway/service-plugins/restart")
async def restart(payload: RestartPluginRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    owner: Annotated[GatewayPluginRuntime, Depends(plugin_owner)],
) -> APIResponse[PluginSlotStatusDTO]:
    response = await relay(request, payload, auth)
    if response is not None:
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail"))
        return APIResponse[PluginSlotStatusDTO].model_validate(response.json())
    require_federation_capability(request, auth, "service_plugin_runtime_manage")
    binding = PluginBinding(package_id=payload.package_id, plugin_id=payload.plugin_id,
        package_version=payload.package_version, enabled=True)
    try:
        async with request.app.state.gateway_config_reload.mutation_guard():
            require_federation_capability(request, auth, "service_plugin_runtime_manage")
            config = request.app.state.gateway_config_reload.config
            if payload.revision != config.revision or binding not in gateway_plugin_bindings(config.payload):
                raise ValueError("原配置绑定已停用或版本／revision 已变化，拒绝重启")
            result = await owner.apply_binding(binding, revision=payload.revision, restart=True)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    data = PluginSlotStatusDTO.model_validate(result)
    return APIResponse[PluginSlotStatusDTO](data=data, request_id=get_request_id(request))


@router.api_route("/api/gateway/service-plugins/{package_id}/{plugin_id}/instances/{instance_id}/services/{service_id}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"], dependencies=[Depends(verify_gateway_token)],
    response_model=None, include_in_schema=False)
async def service_request(package_id: str, plugin_id: str, instance_id: str, service_id: str,
    path: str, package_version: str, request: Request,
    owner: Annotated[GatewayPluginRuntime, Depends(plugin_owner)],
) -> StreamingResponse:
    # TODO：接原 Workspace 认证绑定与联邦业务权限后开放 workspace／Relay。
    binding = PluginBinding(package_id=package_id, plugin_id=plugin_id,
        package_version=package_version, enabled=True)
    if any(part in {"..", "."} for part in path.split("/")):
        raise HTTPException(422, "服务路径不能越过具名服务")
    try:
        base_url, token = owner.service(binding, service_id=service_id, instance_id=instance_id)
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    query = [(key, value) for key, value in request.query_params.multi_items() if key != "package_version"]
    headers = {"X-Local-Token": token, "X-Request-ID": get_request_id(request)}
    if request.headers.get("content-type"):
        headers["Content-Type"] = request.headers["content-type"]
    upstream = owner.client.build_request(request.method, build_upstream_url(base_url, (), path),
        params=tuple(query), content=request.stream(), headers=headers, timeout=None)
    response = await owner.client.send(upstream, stream=True)
    body = UpstreamResponseBody(response)
    return StreamingResponse(body, status_code=response.status_code,
        headers={key: value for key, value in response.headers.items()
            if key.lower() in {"content-type", "cache-control", "etag", "mcp-session-id", "mcp-protocol-version"}},
        background=BackgroundTask(body.aclose))
