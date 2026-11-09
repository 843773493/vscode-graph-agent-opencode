"""自有包管理及精确插件实例路由；不保存工作区业务事实。"""

from __future__ import annotations

import asyncio
import logging
import zipfile
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.runtime_control import verify_local_control_request
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.federation.policy import require_federation_capability
from app.gateway.proxy_upstream import TARGET_GATEWAY_ID_HEADER, parse_target_gateway_id
from app.gateway.runtime.service_plugins.runtime import (
    GatewayPluginRuntime,
    gateway_plugin_bindings,
    workspace_default_bindings,
)
from app.gateway.server.management_routes import (
    check_management_route,
    management_gateway_route,
)
from app.schemas.canonical_params import CanonicalWorkspaceId
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
    WorkspacePluginConfigurationDTO,
)
from app.schemas.gateway.service_plugin_workspace import (
    WorkspacePluginCandidate,
    WorkspacePluginCreation,
)
from app.schemas.internal_v2.common import APIResponse
from configs.plugin_parameters import plugin_parameters

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/internal/service-plugins/workspaces/{workspace_id}/refresh", include_in_schema=False,
    dependencies=[Depends(verify_gateway_token), Depends(verify_local_control_request)])
async def refresh_workspace(workspace_id: CanonicalWorkspaceId, request: Request) -> APIResponse[WorkspacePluginConfigurationDTO]:
    try:
        state = request.app.state.workspace_plugin_bindings.request_refresh(workspace_id)
    except (ValueError, LookupError) as error:
        raise HTTPException(409, str(error)) from error
    return APIResponse(data=state, request_id=get_request_id(request))


@router.post("/api/internal/service-plugins/workspaces/{workspace_id}/validate", include_in_schema=False,
    dependencies=[Depends(verify_gateway_token), Depends(verify_local_control_request)])
async def validate_workspace_bindings(workspace_id: CanonicalWorkspaceId, candidate: WorkspacePluginCandidate,
    request: Request,
) -> APIResponse[bool]:
    # Workspace 已登记原 pending 引用；与卸载串行核安装存在后才允许发布 active。
    registry = request.app.state.registry
    bindings = candidate.bindings
    try:
        async with registry.backend_topology_lock:
            target = registry.resolve((registry.gateway_id, workspace_id))
            if target.connection_kind != "local":
                raise ValueError("插件绑定验证必须属于原生工作区")
            keys = {(binding.package_id, binding.plugin_id) for binding in bindings}
            if len(keys) != len(bindings) or any(binding.workspace_id != workspace_id for binding in bindings):
                raise ValueError("插件绑定必须唯一且属于认证交付的工作区")
            request.app.state.service_plugins.validate_bindings(bindings, workspace_id=workspace_id,
                                                               parameters=candidate.parameters)
    except (ValueError, LookupError, FileNotFoundError) as error:
        raise HTTPException(409, str(error)) from error
    return APIResponse(data=True, request_id=get_request_id(request))


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
        async with request.app.state.gateway_config_reload.mutation_guard(), request.app.state.registry.backend_topology_lock:
            require_federation_capability(request, auth, "service_plugin_installation_manage")
            bindings = gateway_plugin_bindings(request.app.state.gateway_config_reload.config.payload)
            references = owner.references(payload.package_id, payload.package_version)
            references.extend(request.app.state.skill_source_owner.read_references(payload.package_id, payload.package_version))
            references.extend(f"workspace-default:{binding.plugin_id}"
                for binding in workspace_default_bindings(request.app.state.gateway_config_reload.config.payload)
                if binding.package_id == payload.package_id and binding.package_version == payload.package_version)
            for target in request.app.state.registry.targets():
                if target.plugin_creation is not None:
                    creation = WorkspacePluginCreation.model_validate(target.plugin_creation)
                    references.extend(f"workspace-create:{target.workspace_id}:{creation.intent_id}:{binding.plugin_id}"
                        for binding in creation.bindings
                        if binding.package_id == payload.package_id and binding.package_version == payload.package_version)
            pending = request.app.state.gateway_state.get_pending_config_candidate(config_domain="gateway")
            if pending is not None and pending.state not in {"active", "discarded", "rejected"}:
                references.extend(f"candidate:{pending.candidate_id}:{binding.plugin_id}"
                    for binding in [*gateway_plugin_bindings(pending.payload), *workspace_default_bindings(pending.payload)]
                    if binding.package_id == payload.package_id and binding.package_version == payload.package_version)
            coordinator = request.app.state.workspace_plugin_bindings
            package = owner.packages.get(payload.package_id, payload.package_version)
            workspace_bindings = (await coordinator.read_references()
                if any(plugin.instance_scope == "workspace" for plugin in package.manifest.plugins)
                else coordinator.referenced_bindings())
            references.extend(f"workspace-configuration:{binding.workspace_id}:{binding.plugin_id}"
                for binding in workspace_bindings
                if binding.package_id == payload.package_id and binding.package_version == payload.package_version)
            references.extend(f"configuration:{binding.plugin_id}" for binding in bindings
                if binding.package_id == payload.package_id and binding.package_version == payload.package_version)
            await run_sync_shielded(owner.packages.uninstall, payload.package_id, payload.package_version,
                references=references)
    except (ValueError, RuntimeError, LookupError, httpx.HTTPError) as error:
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
            request.app.state.gateway_config_reload.config.payload)]
            + [binding.model_dump(mode="json") for binding in request.app.state.workspace_plugin_bindings.configured_bindings()],
        "workspace_configurations": [state.model_dump(mode="json")
            for state in request.app.state.workspace_plugin_bindings.states.values()]})
    return APIResponse[PluginRuntimeStatusDTO](data=data, request_id=get_request_id(request))


@router.get("/api/gateway/service-plugins/debugger-contributions", include_in_schema=False)
async def debugger_contributions(
    workspace_id: CanonicalWorkspaceId,
    request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    owner: Annotated[GatewayPluginRuntime, Depends(plugin_owner)],
) -> APIResponse[dict[str, object]]:
    """读取本机 Workspace 当前 ready 的 Debug 插件声明，不走管理状态接口。"""

    verify_local_control_request(request)
    if auth.kind != "local":
        raise HTTPException(403, "调试器发现只接受本机 Workspace Backend")
    if request.headers.get("X-BoxTeam-Workspace-Id") != workspace_id:
        raise HTTPException(403, "调试器发现的 Workspace 身份不匹配")
    gateway_id = request.app.state.registry.gateway_id
    if parse_target_gateway_id(request.headers.get(TARGET_GATEWAY_ID_HEADER)) != gateway_id:
        raise HTTPException(409, "调试器发现必须指向当前原生 Gateway")
    try:
        target = request.app.state.registry.resolve((gateway_id, workspace_id))
    except LookupError as error:
        raise HTTPException(409, "调试器发现的 Workspace 未登记在当前 Gateway") from error
    if target.connection_kind != "local":
        raise HTTPException(409, "调试器发现只支持当前 Gateway 的原生 Workspace")

    debuggers: list[dict[str, object]] = []
    for slot in owner.slots.values():
        binding, receipt = slot.binding, slot.receipt
        if (
            binding.workspace_id != workspace_id
            or not binding.enabled
            or slot.state != "ready"
            or slot.process is None
            or not slot.process.is_alive()
            or receipt is None
            or receipt.status != "ready"
            or receipt.workspace_id != workspace_id
            or receipt.gateway_id != gateway_id
            or slot.stopped.is_set()
        ):
            continue
        package = owner.packages.get(binding.package_id, binding.package_version)
        for debugger in slot.declaration.contributes.debuggers:
            service = next((item for item in receipt.services if item.service_id == debugger.service_id), None)
            if service is None or service.service_type != "debug" or service.protocol != "dap":
                continue
            debuggers.append({
                **debugger.model_dump(mode="json"),
                "registration_id": f"node-debugger:{receipt.instance_id}:{slot.revision}:{debugger.type}",
                "gateway_id": gateway_id,
                "workspace_id": workspace_id,
                "package_id": binding.package_id,
                "plugin_id": binding.plugin_id,
                "package_version": binding.package_version,
                "package_digest": package.digest,
                "instance_id": receipt.instance_id,
                "contract_version": 1,
                "declaration_revision": slot.revision,
            })
    debuggers.sort(key=lambda item: (
        str(item["package_id"]), str(item["plugin_id"]), str(item["type"]),
    ))
    data = {"gateway_id": gateway_id, "workspace_id": workspace_id, "debuggers": debuggers}
    return APIResponse[dict[str, object]](data=data, request_id=get_request_id(request))


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
        package_version=payload.package_version, enabled=True, workspace_id=payload.workspace_id)
    try:
        if payload.workspace_id is not None:
            async with request.app.state.registry.backend_topology_lock:
                coordinator = request.app.state.workspace_plugin_bindings
                target = request.app.state.registry.resolve((payload.gateway_id, payload.workspace_id))
                snapshot = await coordinator.read(target)
                if payload.revision != snapshot.revision or binding not in snapshot.bindings:
                    raise ValueError("工作区原配置已停用或版本／revision 已变化，拒绝重启")
                require_federation_capability(request, auth, "service_plugin_runtime_manage")
                result = await owner.apply_binding(binding, revision=payload.revision, restart=True,
                    parameters=snapshot.parameters.get(binding.package_id, {}).get(binding.plugin_id, {}))
            return APIResponse(data=PluginSlotStatusDTO.model_validate(result), request_id=get_request_id(request))
        async with request.app.state.gateway_config_reload.mutation_guard():
            require_federation_capability(request, auth, "service_plugin_runtime_manage")
            config = request.app.state.gateway_config_reload.config
            if payload.revision != config.revision or binding not in gateway_plugin_bindings(config.payload):
                raise ValueError("原配置绑定已停用或版本／revision 已变化，拒绝重启")
            result = await owner.apply_binding(binding, revision=payload.revision, restart=True,
                parameters=plugin_parameters(config.payload).get(binding.package_id, {}).get(binding.plugin_id, {}))
    except (ValueError, LookupError) as error:
        raise HTTPException(409, str(error)) from error
    data = PluginSlotStatusDTO.model_validate(result)
    return APIResponse[PluginSlotStatusDTO](data=data, request_id=get_request_id(request))
