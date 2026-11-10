"""Workspace 插件绑定的内部 owner 交付入口。"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Secret

from app.api.deps import get_workspace_container
from app.api.runtime_control import (
    verify_local_control_request,
    verify_runtime_control_token,
)
from app.container import AppContainer
from app.core.trace_middleware import get_request_id, require_current_gateway_id
from app.schemas.gateway.plugin_private_inputs import (
    PluginParameterDelivery,
    PluginParameterDeliveryRequest,
)
from app.schemas.gateway.service_plugin_workspace import (
    WorkspacePluginCreation,
    WorkspacePluginSnapshot,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.config import ConfigSourceEditRequest
from app.services.infrastructure.config.plugin_private_inputs import (
    PluginPrivateInputStore,
)
from app.services.infrastructure.config.source_document import set_source_value
from configs.plugin_parameters import plugin_parameters

router = APIRouter(
    prefix="/api/internal/service-plugins",
    include_in_schema=False,
    dependencies=[Depends(verify_local_control_request), Depends(verify_runtime_control_token)],
)


@router.post("/parameters", response_model=APIResponse[PluginParameterDelivery])
async def workspace_plugin_parameters(
    payload: PluginParameterDeliveryRequest,
    response: Response,
    request_id: Annotated[str, Depends(get_request_id)],
    container: Annotated[AppContainer, Depends(get_workspace_container)],
) -> APIResponse[PluginParameterDelivery]:
    gateway_id = require_current_gateway_id("Workspace 插件启动参数交付")
    service = container.config_service
    with service.publication_lock:
        active = service.get_snapshot()
        bindings, _ = service.get_workspace_plugin_binding_snapshot()
        if active.revision != payload.revision or payload.binding not in bindings:
            raise HTTPException(409, "插件参数交付的原绑定或配置 revision 已失效")
        binding = payload.binding
        values = plugin_parameters(active.to_dict()).get(binding.package_id, {}).get(binding.plugin_id, {})
        store = PluginPrivateInputStore(directory=container.workspace_root / ".boxteam/credentials/plugins",
            gateway_id=gateway_id, workspace_id=container.workspace_id)
        try:
            resolved = store.resolve(values, package_id=binding.package_id, plugin_id=binding.plugin_id,
                schema=payload.configuration_schema)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    response.headers["Cache-Control"] = "no-store"
    return APIResponse(data=PluginParameterDelivery(values=Secret({binding.package_id: {binding.plugin_id: resolved}})),
        request_id=request_id)


@router.get("/bindings", response_model=APIResponse[WorkspacePluginSnapshot])
async def workspace_plugin_bindings(
    request: Request,
    request_id: Annotated[str, Depends(get_request_id)],
    container: Annotated[AppContainer, Depends(get_workspace_container)],
) -> APIResponse[WorkspacePluginSnapshot]:
    require_current_gateway_id("Workspace 插件绑定交付")
    backend_id = getattr(request.app.state, "backend_id", None)
    backend_generation = os.environ.get("BOXTEAM_BACKEND_GENERATION")
    if not isinstance(backend_id, str) or not backend_id or not backend_generation:
        raise HTTPException(status_code=503, detail="Backend 插件绑定 owner 身份尚未就绪")
    config_service = container.config_service
    with config_service.publication_lock:
        active = config_service.get_snapshot()
        bindings, referenced_bindings = config_service.get_workspace_plugin_binding_snapshot()
        snapshot = WorkspacePluginSnapshot(
            parameters=plugin_parameters(active.to_dict()),
            workspace_id=container.workspace_id,
            backend_id=backend_id,
            backend_generation=backend_generation,
            revision=active.revision,
            bindings=bindings,
            referenced_bindings=referenced_bindings,
        )
    return APIResponse(data=snapshot, request_id=request_id)


@router.post("/initialize", response_model=APIResponse[bool])
async def initialize_workspace_plugins(
    payload: WorkspacePluginCreation,
    request_id: Annotated[str, Depends(get_request_id)],
    container: Annotated[AppContainer, Depends(get_workspace_container)],
) -> APIResponse[bool]:
    """只在创建交接中初始化；同一 intent 复用原候选链，绝不覆盖后续编辑。"""
    gateway_id = require_current_gateway_id("Workspace 创建默认插件")
    service = container.config_service
    source_key = "workspace_root_mutable_override"
    document = service.get_source_document(source_key=source_key, gateway_id=gateway_id,
        idempotency_key=payload.intent_id)
    if document.edit is not None:
        if document.edit.state == "active":
            return APIResponse(data=True, request_id=request_id)
        if document.edit.state not in {"rejected", "candidate_validated"}:
            raise HTTPException(409, "工作区创建默认插件尚未提交，沿原配置候选处理后重试")
        saved = await service.retry_source_edit(source_key=source_key, gateway_id=gateway_id,
            idempotency_key=payload.intent_id)
    else:
        entries = [binding.model_dump(mode="json", exclude={"workspace_id"}) for binding in payload.bindings]
        text = set_source_value(document.jsonc, ("plugins", "runtime", "entries"), entries)
        saved = await service.save_source_edit(source_key=source_key, gateway_id=gateway_id,
            request=ConfigSourceEditRequest(
                gateway_id=gateway_id, workspace_id=container.workspace_id, source_key=source_key,
                jsonc=text, base_file_digest=document.file_digest,
                base_layer_revision=document.layer_revision, base_layer_digest=document.layer_digest,
                expected_active_revision=document.active_revision, expected_active_digest=document.active_digest,
                idempotency_key=payload.intent_id,
            ))
    if saved.edit is None or saved.edit.state != "active":
        raise HTTPException(409, "工作区创建默认插件未完成 active 提交")
    return APIResponse(data=True, request_id=request_id)
