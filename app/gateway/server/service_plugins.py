"""自有包管理及精确插件实例路由；不保存工作区业务事实。"""

from __future__ import annotations

import asyncio
import logging
import zipfile
from collections.abc import AsyncIterator
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.api.runtime_control import verify_local_control_request
from app.api.sse_heartbeat import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    stream_sse_with_heartbeat,
)
from app.core.async_channel import run_channel
from app.core.shielded_cleanup import run_sync_shielded
from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.federation.policy import (
    federation_capability_allowed,
    require_federation_capability,
)
from app.gateway.plugins.sources import enabled_source, read_catalog, source_status
from app.gateway.proxy_upstream import (
    TARGET_GATEWAY_ID_HEADER,
    UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS,
    UpstreamResponseBody,
    parse_target_gateway_id,
    send_upstream_request,
)
from app.gateway.runtime.service_plugins.runtime import (
    GatewayPluginRuntime,
    gateway_plugin_bindings,
    workspace_default_bindings,
)
from app.gateway.server.management_routes import (
    check_management_route,
    management_gateway_route,
)
from app.gateway.server.service_plugin_proxy import (
    _PluginSnapshotResponse,
    plugin_gateway_route,
)
from app.schemas.canonical_params import CanonicalWorkspaceId
from app.schemas.gateway.plugin_installation import (
    InstallPluginRequest,
    PluginInstallOperationDTO,
    PluginInstallOperationRequest,
    PluginInventoryResultDTO,
    PluginSourceCatalogDTO,
    PluginSourceCatalogRequest,
)
from app.schemas.gateway.plugin_private_inputs import (
    PluginParameterSchema,
    PluginPrivateInputReference,
    PluginPrivateInputWriteRequest,
    WorkspacePluginSchemas,
)
from app.schemas.gateway.service_plugin import (
    InstalledPluginPackageDTO,
    PluginBinding,
    PluginConfigurationSchemaDTO,
    PluginConfigurationSchemasDTO,
    PluginConfigurationSchemasRequest,
    PluginRuntimeStatusDTO,
    PluginSlotStatusDTO,
    PluginTarget,
    PluginUninstallResultDTO,
    PluginVersionRequest,
    RestartPluginRequest,
    WorkspacePluginConfigurationDTO,
)
from app.schemas.gateway.service_plugin_workspace import (
    WorkspacePluginCandidateApply,
    WorkspacePluginCreation,
    WorkspacePluginValidation,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.sse import sse_responses
from app.services.infrastructure.config.plugin_private_inputs import (
    validate_private_input,
)
from configs.plugin_parameters import plugin_parameters

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/internal/service-plugins/workspaces/{workspace_id}/schemas", include_in_schema=False,
    dependencies=[Depends(verify_gateway_token), Depends(verify_local_control_request)])
async def workspace_parameter_schemas(workspace_id: CanonicalWorkspaceId, bindings: list[PluginBinding],
    request: Request,
) -> APIResponse[WorkspacePluginSchemas]:
    registry = request.app.state.registry
    try:
        target = registry.resolve((registry.gateway_id, workspace_id))
        if target.connection_kind != "local" or any(binding.workspace_id != workspace_id for binding in bindings):
            raise ValueError("插件参数声明必须属于认证交付的原生工作区")
        owner = request.app.state.service_plugins
        plugins = [PluginParameterSchema(binding=binding,
            configuration_schema=owner.declaration(binding).contributes.configuration) for binding in bindings]
    except (ValueError, LookupError, FileNotFoundError) as error:
        raise HTTPException(409, str(error)) from error
    return APIResponse(data=WorkspacePluginSchemas(gateway_id=registry.gateway_id,
        workspace_id=workspace_id, plugins=plugins), request_id=get_request_id(request))


@router.post("/api/internal/service-plugins/workspaces/{workspace_id}/refresh", include_in_schema=False,
    dependencies=[Depends(verify_gateway_token), Depends(verify_local_control_request)])
async def refresh_workspace(workspace_id: CanonicalWorkspaceId, request: Request,
    wait_for_ready: bool = False,
) -> APIResponse[WorkspacePluginConfigurationDTO]:
    try:
        if wait_for_ready:
            state = await request.app.state.workspace_plugin_bindings.refresh_startup(
                workspace_id, request.headers.get("X-BoxTeam-Backend-Generation", ""),
            )
            if state.state != "ready":
                raise ValueError(state.error or "Workspace 插件启动交接未就绪")
        else:
            state = request.app.state.workspace_plugin_bindings.request_refresh(workspace_id)
    except (ValueError, LookupError) as error:
        raise HTTPException(409, str(error)) from error
    return APIResponse(data=state, request_id=get_request_id(request))


@router.post("/api/internal/service-plugins/workspaces/{workspace_id}/validate", include_in_schema=False,
    dependencies=[Depends(verify_gateway_token), Depends(verify_local_control_request)])
async def validate_workspace_bindings(workspace_id: CanonicalWorkspaceId, candidate: WorkspacePluginValidation,
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
                parameters=candidate.parameters, delivered=candidate.delivery.values.get_secret_value())
    except (ValueError, LookupError, FileNotFoundError) as error:
        raise HTTPException(409, str(error)) from error
    return APIResponse(data=True, request_id=get_request_id(request))


@router.post("/api/internal/service-plugins/workspaces/{workspace_id}/apply-candidate", include_in_schema=False,
    dependencies=[Depends(verify_gateway_token), Depends(verify_local_control_request)])
async def apply_workspace_candidate(workspace_id: CanonicalWorkspaceId, candidate: WorkspacePluginCandidateApply,
    request: Request,
) -> APIResponse[PluginSlotStatusDTO]:
    try:
        if candidate.binding.workspace_id != workspace_id:
            raise ValueError("插件参数候选必须属于认证交付的原生工作区")
        bindings = candidate.bindings
        if (len({(item.package_id, item.plugin_id) for item in bindings}) != len(bindings)
                or any(item.workspace_id != workspace_id for item in bindings)):
            raise ValueError("插件候选绑定必须唯一且属于认证工作区")
        request.app.state.service_plugins.validate_bindings(
            bindings, workspace_id=workspace_id, parameters=candidate.parameters,
            delivered=candidate.delivery.values.get_secret_value(),
        )
        parameters = candidate.parameters.get(candidate.binding.package_id, {}).get(candidate.binding.plugin_id, {})
        delivered_parameters = candidate.delivery.values.get_secret_value().get(
            candidate.binding.package_id, {}
        ).get(candidate.binding.plugin_id, {})
        result = await request.app.state.workspace_plugin_bindings.apply_candidate(
            binding=candidate.binding, candidate_revision=candidate.candidate_revision,
            expected_revision=candidate.expected_revision, expected_binding=candidate.expected_binding,
            bindings=bindings, parameters=parameters, delivered_parameters=delivered_parameters,
        )
        if result["state"] != "ready":
            raise RuntimeError("插件参数候选未启动就绪；原 apply journal 保留实际失败")
    except (ValueError, LookupError, RuntimeError, FileNotFoundError) as error:
        raise HTTPException(409, str(error)) from error
    return APIResponse(data=PluginSlotStatusDTO.model_validate(result), request_id=get_request_id(request))


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


@router.post("/api/gateway/service-plugins/private-inputs", response_model=APIResponse[PluginPrivateInputReference])
async def write_gateway_private_input(payload: PluginPrivateInputWriteRequest, request: Request, response: Response,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    owner: Annotated[GatewayPluginRuntime, Depends(plugin_owner)],
) -> APIResponse[PluginPrivateInputReference]:
    remote = await relay(request, payload, auth)
    if remote is not None:
        if remote.is_error:
            raise HTTPException(remote.status_code, remote.json().get("detail"))
        response.headers["Cache-Control"] = "no-store"
        return APIResponse[PluginPrivateInputReference].model_validate_json(remote.content)
    require_federation_capability(request, auth, "configuration_write")
    if payload.gateway_id != owner.gateway_id or payload.workspace_id is not None:
        raise HTTPException(409, "Gateway 私有输入必须属于当前原生配置 owner")
    try:
        document = request.app.state.gateway_config_reload.get_source_document(source_key=payload.source_key)
        if not document.writable:
            raise ValueError("插件私有参数所选配置来源只读")
        binding = PluginBinding(package_id=payload.package_id, plugin_id=payload.plugin_id,
            package_version=payload.package_version, enabled=False)
        schema = owner.declaration(binding).contributes.configuration
        values = plugin_parameters(document.source_effective).get(payload.package_id, {}).get(payload.plugin_id, {})
        validate_private_input(schema, values, payload.parameter_pointer, payload.value.get_secret_value())
        reference = await run_sync_shielded(lambda: owner.private_inputs.write(package_id=payload.package_id,
            plugin_id=payload.plugin_id, parameter_pointer=payload.parameter_pointer, value=payload.value))
    except (ValueError, LookupError, FileNotFoundError) as error:
        raise HTTPException(409, str(error)) from error
    require_federation_capability(request, auth, "configuration_write")
    response.headers["Cache-Control"] = "no-store"
    return APIResponse(data=reference, request_id=get_request_id(request))


@router.post("/api/gateway/service-plugins/configuration-schemas")
async def configuration_schemas(payload: PluginConfigurationSchemasRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    owner: Annotated[GatewayPluginRuntime, Depends(plugin_owner)],
) -> APIResponse[PluginConfigurationSchemasDTO]:
    response = await relay(request, payload, auth)
    if response is not None:
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail"))
        return APIResponse[PluginConfigurationSchemasDTO].model_validate_json(response.content)
    _configuration_schema_access(payload, request, auth)
    return APIResponse(data=await _configuration_schema_snapshot(payload, request, auth, owner),
                       request_id=get_request_id(request))


def _configuration_schema_access(payload: PluginConfigurationSchemasRequest, request: Request,
                                 auth: GatewayAuthContext) -> None:
    require_federation_capability(request, auth, "configuration_read")
    if payload.workspace_id is not None:
        try:
            target = request.app.state.registry.resolve((payload.gateway_id, payload.workspace_id))
        except LookupError as error:
            raise HTTPException(409, "配置 schema 所属工作区未登记在当前 Gateway") from error
        if target.connection_kind != "local":
            raise HTTPException(409, "配置 schema 必须由工作区所属的原生 Gateway 提供")


async def _configuration_schema_snapshot(payload: PluginConfigurationSchemasRequest, request: Request,
                                         auth: GatewayAuthContext, owner: GatewayPluginRuntime,
) -> PluginConfigurationSchemasDTO:
    scope = "workspace" if payload.workspace_id is not None else "gateway"
    packages = await asyncio.to_thread(owner.packages.inventory)
    plugins = [PluginConfigurationSchemaDTO(package_id=package.manifest.package_id,
        package_version=package.manifest.version, plugin_id=plugin.id, name=plugin.name,
        instance_scope=plugin.instance_scope, configuration_schema=plugin.contributes.configuration)
        for package in packages for plugin in package.manifest.plugins
        if plugin.instance_scope == scope and plugin.contributes.configuration is not None]
    _configuration_schema_access(payload, request, auth)
    return PluginConfigurationSchemasDTO(gateway_id=owner.gateway_id,
        workspace_id=payload.workspace_id, plugins=plugins)


@router.post("/api/gateway/service-plugins/configuration-schemas/events", response_class=StreamingResponse,
    responses=sse_responses("目标安装域的完整配置声明快照", {"snapshot": PluginConfigurationSchemasDTO}))
async def configuration_schema_events(payload: PluginConfigurationSchemasRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    owner: Annotated[GatewayPluginRuntime, Depends(plugin_owner)],
) -> StreamingResponse:
    references = ExitStack()
    body: UpstreamResponseBody | None = None
    handed_off = False
    try:
        remote = references.enter_context(plugin_gateway_route(request, payload, auth))
        require_federation_capability(request, auth, "configuration_read")
        if remote is None:
            _configuration_schema_access(payload, request, auth)
            stopped, changed = asyncio.Event(), asyncio.Event()
            loop = asyncio.get_running_loop()

            def installed() -> None:
                loop.call_soon_threadsafe(changed.set)

            def registered() -> None:
                try:
                    _configuration_schema_access(payload, request, auth)
                except (HTTPException, LookupError):
                    loop.call_soon_threadsafe(stopped.set)

            owner.packages.add_change_observer(installed)
            references.callback(owner.packages.remove_change_observer, installed)
            registry = request.app.state.registry
            registry.add_commit_observer(registered)
            references.callback(registry.remove_commit_observer, registered)

            async def installed_change() -> None:
                await changed.wait()

            async def frames() -> AsyncIterator[str]:
                while not stopped.is_set():
                    # 先清唤醒再读原库存，读取期间提交的发布不会丢失。
                    changed.clear()
                    policy_changed = request.app.state.federation_policy_store.changed
                    if not federation_capability_allowed(request, auth, "configuration_read"):
                        return
                    snapshot = await _configuration_schema_snapshot(payload, request, auth, owner)
                    if stopped.is_set():
                        return
                    yield f"event: snapshot\ndata: {snapshot.model_dump_json()}\n\n"
                    await run_channel(installed_change(), policy_changed.wait())

            source = stream_sse_with_heartbeat(frames(), heartbeat_interval_seconds=SSE_HEARTBEAT_INTERVAL_SECONDS,
                                               close_source_on_exit=True)
        else:
            client = request.app.state.http_client
            upstream = client.build_request("POST", remote.url, headers=remote.headers,
                json=payload.model_dump(mode="json", exclude={"gateway_connection_id"}), timeout=None)
            response = await send_upstream_request(client, upstream,
                response_headers_timeout_seconds=UPSTREAM_RESPONSE_HEADERS_TIMEOUT_SECONDS)
            body = UpstreamResponseBody(response)
            if response.is_error:
                await response.aread()
                raise HTTPException(response.status_code, response.json().get("detail"))
            if (response.headers.get("X-Request-ID") != get_request_id(request)
                or response.headers.get("content-type", "").split(";", 1)[0] != "text/event-stream"):
                raise ValueError("配置声明事件的请求身份或协议不符")
            source, stopped = body, remote.stopped
        result = _PluginSnapshotResponse(source, request=request, auth=auth, stopped=stopped,
            close=references.close, body=body, capabilities=("configuration_read",))
        handed_off = True
        return result
    except LookupError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    finally:
        if not handed_off:
            try:
                if body is not None:
                    await body.aclose()
            finally:
                references.close()


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
    data = PluginInventoryResultDTO(packages=[InstalledPluginPackageDTO.model_validate(package.model_dump()) for package in packages],
        sources=source_status(request.app.state.gateway_config_reload.config.payload))
    return APIResponse[PluginInventoryResultDTO](data=data, request_id=get_request_id(request))


@router.post("/api/gateway/service-plugins/install")
async def install(payload: InstallPluginRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    owner: Annotated[GatewayPluginRuntime, Depends(plugin_owner)],
) -> APIResponse[PluginInstallOperationDTO]:
    # 原生路径是明确目标机器参数；联邦调用不能借本跳路径安装。
    response = await relay(request, payload, auth)
    if response is not None:
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail"))
        return APIResponse[PluginInstallOperationDTO].model_validate(response.json())
    require_federation_capability(request, auth, "service_plugin_installation_manage")
    if payload.source_id is None and auth.kind != "local":
        raise HTTPException(422, "远端安装必须选择自有来源制品，不接受远端原生路径安装")
    if payload.path and not Path(payload.path).is_absolute():
        raise HTTPException(422, "本地包入口必须是目标机器的明确绝对路径")
    try:
        operation = await request.app.state.plugin_installations.start(payload)
    except (ValueError, FileNotFoundError, zipfile.BadZipFile) as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    require_federation_capability(request, auth, "service_plugin_installation_manage")
    return APIResponse(data=operation, request_id=get_request_id(request))


@router.post("/api/gateway/service-plugins/install-operation")
async def installation_operation(payload: PluginInstallOperationRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
) -> APIResponse[PluginInstallOperationDTO]:
    response = await relay(request, payload, auth)
    if response is not None:
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail"))
        return APIResponse[PluginInstallOperationDTO].model_validate_json(response.content)
    require_federation_capability(request, auth, "service_plugin_installation_manage")
    try:
        operation = request.app.state.plugin_installations.get(payload.operation_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    return APIResponse(data=operation, request_id=get_request_id(request))


@router.post("/api/gateway/service-plugins/cancel-install")
async def cancel_installation(payload: PluginInstallOperationRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
) -> APIResponse[PluginInstallOperationDTO]:
    response = await relay(request, payload, auth)
    if response is not None:
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail"))
        return APIResponse[PluginInstallOperationDTO].model_validate_json(response.content)
    require_federation_capability(request, auth, "service_plugin_installation_manage")
    try:
        operation = request.app.state.plugin_installations.cancel(payload.operation_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    return APIResponse(data=operation, request_id=get_request_id(request))


@router.post("/api/gateway/service-plugins/source-catalog")
async def source_catalog(payload: PluginSourceCatalogRequest, request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
) -> APIResponse[PluginSourceCatalogDTO]:
    response = await relay(request, payload, auth)
    if response is not None:
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail"))
        return APIResponse[PluginSourceCatalogDTO].model_validate_json(response.content)
    require_federation_capability(request, auth, "service_plugin_installation_manage")
    try:
        source = enabled_source(request.app.state.gateway_config_reload.config.payload, payload.source_id)
        catalog = await read_catalog(request.app.state.plugin_download_client,
            gateway_id=request.app.state.registry.gateway_id, source=source, request=payload)
        if enabled_source(request.app.state.gateway_config_reload.config.payload, payload.source_id) != source:
            raise HTTPException(409, "插件来源已变化，请重新查询目录")
    except (ValueError, LookupError) as error:
        raise HTTPException(422, str(error)) from error
    except httpx.HTTPError as error:
        detail = (f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError)
            else type(error).__name__)
        raise HTTPException(502, f"插件来源目录请求失败: {detail}") from error
    require_federation_capability(request, auth, "service_plugin_installation_manage")
    return APIResponse(data=catalog, request_id=get_request_id(request))


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
            for pending in request.app.state.gateway_state.list_pending_config_candidates(config_domain="gateway"):
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
            owner.release_uninstalled(payload.package_id, payload.package_version)
    except (ValueError, RuntimeError, LookupError, FileNotFoundError, httpx.HTTPError) as error:
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
                slot = owner.slots.get(owner.key(binding))
                if slot is None or payload.revision != slot.revision or slot.binding != binding:
                    raise ValueError("插件实例 revision 已变化，拒绝重启")
                snapshot = await coordinator.read(target)
                key = (binding.package_id, binding.plugin_id, binding.workspace_id)
                current_binding = next((item for item in snapshot.bindings if (
                    item.package_id, item.plugin_id, item.workspace_id,
                ) == key), None)
                if (current_binding is None or not current_binding.enabled
                        or (current_binding.package_id, current_binding.plugin_id,
                            current_binding.package_version, current_binding.workspace_id)
                        != (binding.package_id, binding.plugin_id, binding.package_version, binding.workspace_id)):
                    raise ValueError("插件实例 revision 已变化或 Workspace 原配置绑定已停用，拒绝重启")
                require_federation_capability(request, auth, "service_plugin_runtime_manage")
                result = await owner.apply_binding(current_binding, revision=snapshot.revision, restart=True,
                    parameters=snapshot.parameters.get(binding.package_id, {}).get(binding.plugin_id, {}))
            return APIResponse(data=PluginSlotStatusDTO.model_validate(result), request_id=get_request_id(request))
        async with request.app.state.gateway_config_reload.mutation_guard():
            require_federation_capability(request, auth, "service_plugin_runtime_manage")
            config = request.app.state.gateway_config_reload.config
            slot = owner.slots.get(owner.key(binding))
            current_bindings = gateway_plugin_bindings(config.payload)
            current_binding = next((item for item in current_bindings if (
                item.package_id, item.plugin_id, item.workspace_id,
            ) == (binding.package_id, binding.plugin_id, binding.workspace_id)), None)
            if (slot is None or slot.revision != payload.revision or slot.binding != binding
                    or current_binding != binding):
                raise ValueError("原配置绑定已停用或版本／revision 已变化，拒绝重启")
            result = await owner.apply_binding(binding, revision=config.revision, restart=True,
                parameters=plugin_parameters(config.payload).get(binding.package_id, {}).get(binding.plugin_id, {}))
    except (ValueError, LookupError) as error:
        raise HTTPException(409, str(error)) from error
    data = PluginSlotStatusDTO.model_validate(result)
    return APIResponse[PluginSlotStatusDTO](data=data, request_id=get_request_id(request))
