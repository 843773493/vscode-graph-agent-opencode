from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.deps import (
    get_config_service,
    get_request_id,
    get_workspace_container,
    verify_local_token,
)
from app.api.errors import state_conflict_http_error
from app.api.sse_heartbeat import SSE_NO_CACHE_HEADERS
from app.container import AppContainer
from app.core.trace_middleware import require_current_gateway_id
from app.schemas.gateway.plugin_private_inputs import (
    PluginPrivateInputReference,
    PluginPrivateInputWriteRequest,
)
from app.schemas.gateway.service_plugin import PluginBinding
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.config import (
    ConfigDTO,
    ConfigEventDTO,
    ConfigEventsDTO,
    ConfigPendingDiscardRequest,
    ConfigPendingHealthProofRequest,
    ConfigReloadStatusDTO,
    ConfigRestartFailureRequest,
    ConfigSourceDocumentDTO,
    ConfigSourceDTO,
    ConfigSourceEditRequest,
    ConfigSourcePreflightDTO,
    ConfigSourcesDTO,
    ConfigStartupContractDTO,
    ConfigUpdateRequest,
)
from app.schemas.internal_v2.sse import SseErrorDTO, sse_responses
from app.services.infrastructure.config.plugin_private_inputs import (
    PluginPrivateInputStore,
    validate_private_input,
)
from app.services.infrastructure.config.policy import workspace_config_policy
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventCursorGoneError,
)
from app.services.infrastructure.config_service import ConfigService
from configs.plugin_parameters import plugin_parameters

router = APIRouter(prefix="/config", tags=["config"])


@router.post("/plugin-private-inputs", response_model=APIResponse[PluginPrivateInputReference])
async def write_plugin_private_input(
    payload: PluginPrivateInputWriteRequest,
    response: Response,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    container: Annotated[AppContainer, Depends(get_workspace_container)],
) -> APIResponse[PluginPrivateInputReference]:
    gateway_id = require_current_gateway_id("插件私有参数写入")
    if payload.gateway_id != gateway_id or payload.workspace_id != container.workspace_id:
        raise HTTPException(409, "插件私有输入目标与原配置 owner 不一致")
    service = container.config_service
    try:
        document = service.get_source_document(source_key=payload.source_key, gateway_id=gateway_id)
        if not document.writable:
            raise ValueError("插件私有参数所选配置来源只读")
        binding = PluginBinding(package_id=payload.package_id, plugin_id=payload.plugin_id,
            package_version=payload.package_version, workspace_id=container.workspace_id, enabled=False)
        declarations = await service.get_workspace_plugin_schemas([binding], gateway_id=gateway_id)
        values = plugin_parameters(document.source_effective).get(payload.package_id, {}).get(payload.plugin_id, {})
        validate_private_input(declarations.plugins[0].configuration_schema, values,
            payload.parameter_pointer, payload.value.get_secret_value())
        store = PluginPrivateInputStore(directory=container.workspace_root / ".boxteam/credentials/plugins",
            gateway_id=gateway_id, workspace_id=container.workspace_id)
        reference = store.write(package_id=payload.package_id, plugin_id=payload.plugin_id,
            parameter_pointer=payload.parameter_pointer, value=payload.value)
    except (ValueError, LookupError, FileNotFoundError, ConfigConflictError) as error:
        raise HTTPException(409, str(error)) from error
    response.headers["Cache-Control"] = "no-store"
    return APIResponse(data=reference, request_id=request_id)


def _service_plugin_runtime_manage(request: Request) -> bool:
    value = request.headers.get("x-boxteam-service-plugin-runtime-manage")
    if value is None:
        return True
    if value == "true":
        return True
    if value == "false":
        return False
    raise HTTPException(status_code=400, detail="插件运行绑定授权头必须为 true 或 false")


@router.get("", response_model=APIResponse[ConfigDTO], summary="获取配置")
async def get_config(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    result = await config_service.get()
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/reload-status",
    response_model=APIResponse[ConfigReloadStatusDTO],
    summary="获取配置热重载状态",
)
async def get_config_reload_status(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    return APIResponse(
        data=config_service.get_reload_status_dto(),
        request_id=request_id,
    )


@router.get(
    "/sources",
    response_model=APIResponse[ConfigSourcesDTO],
    summary="获取配置来源",
)
async def get_config_sources(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    revision, _schema_path, sources = config_service.get_source_diagnostics()
    return APIResponse(
        data=ConfigSourcesDTO(
            revision=revision,
            schema_uri=config_service.get_schema_source_uri(),
            sources=[
                ConfigSourceDTO(
                    uri=source.uri,
                    layer=source.layer,
                    precedence=source.precedence,
                    loaded=source.loaded,
                    source_key=config_service.get_source_document_key(layer=source.layer, precedence=source.precedence),
                    presence=source.presence,
                    layer_revision=source.layer_revision,
                    layer_digest=source.layer_digest,
                    source_generation=source.source_generation,
                )
                for source in sources
            ],
            runtime_overrides=list(config_service.get_runtime_override_keys()),
            policy_manifest=list(workspace_config_policy().policy_manifest()),
        ),
        request_id=request_id,
    )


@router.get("/sources/{source_key}", response_model=APIResponse[ConfigSourceDocumentDTO], summary="读取配置来源正文")
async def get_config_source_document(
    source_key: str,
    config_service: Annotated[ConfigService, Depends(get_config_service)],
    idempotency_key: str | None = Query(default=None),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
):
    try:
        result = config_service.get_source_document(source_key=source_key,
            gateway_id=require_current_gateway_id("配置来源读取"), idempotency_key=idempotency_key)
    except ConfigConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/sources/{source_key}/preflight", response_model=APIResponse[ConfigSourcePreflightDTO], summary="预检配置来源修改")
async def preflight_config_source(
    source_key: str,
    payload: ConfigSourceEditRequest,
    config_service: Annotated[ConfigService, Depends(get_config_service)],
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
):
    try:
        result = await config_service.preflight_source_edit(source_key=source_key,
            gateway_id=require_current_gateway_id("配置来源预检"), request=payload)
    except ConfigConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.put("/sources/{source_key}", response_model=APIResponse[ConfigSourceDocumentDTO], summary="保存指定配置来源文件")
async def save_config_source(
    source_key: str,
    payload: ConfigSourceEditRequest,
    request: Request,
    config_service: Annotated[ConfigService, Depends(get_config_service)],
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
):
    try:
        result = await config_service.save_source_edit(source_key=source_key,
            gateway_id=require_current_gateway_id("配置来源保存"), request=payload,
            service_plugin_runtime_manage=_service_plugin_runtime_manage(request))
    except ConfigConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/pending/startup-contract",
    response_model=APIResponse[ConfigStartupContractDTO],
    summary="获取 Workspace pending 启动契约",
)
async def get_pending_startup_contract(
    candidate_ref: str = Query(..., min_length=1),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    try:
        contract = config_service.get_pending_startup_contract(
            candidate_ref=candidate_ref
        )
    except (ConfigConflictError, ValueError) as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(
        data=ConfigStartupContractDTO.model_validate(contract),
        request_id=request_id,
    )


@router.post(
    "/pending/restart-failed",
    response_model=APIResponse[ConfigReloadStatusDTO],
    summary="记录 Workspace pending 重启失败",
)
async def record_pending_restart_failure(
    payload: ConfigRestartFailureRequest,
    candidate_ref: str = Query(..., min_length=1),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    try:
        config_service.record_pending_restart_failure(
            candidate_ref=candidate_ref,
            error=payload.error,
            old_runtime_recovered=payload.old_runtime_recovered,
        )
    except (ConfigConflictError, ValueError) as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=config_service.get_reload_status_dto(), request_id=request_id)


@router.post(
    "/pending/retry",
    response_model=APIResponse[ConfigReloadStatusDTO],
    summary="重试 Workspace pending 重启",
)
async def retry_pending_restart(
    candidate_ref: str = Query(..., min_length=1),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    try:
        config_service.retry_pending_restart(candidate_ref=candidate_ref)
    except (ConfigConflictError, ValueError) as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=config_service.get_reload_status_dto(), request_id=request_id)


@router.post(
    "/pending/resolve",
    response_model=APIResponse[ConfigReloadStatusDTO],
    summary="用匹配的健康证明恢复 Workspace pending",
)
async def resolve_pending_restart(
    payload: ConfigPendingHealthProofRequest,
    candidate_ref: str = Query(..., min_length=1),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    try:
        config_service.resolve_pending_restart(
            candidate_ref=candidate_ref,
            health_proof=payload.model_dump(exclude_none=True),
        )
    except (ConfigConflictError, ValueError) as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=config_service.get_reload_status_dto(), request_id=request_id)


@router.post(
    "/pending/discard",
    response_model=APIResponse[ConfigReloadStatusDTO],
    summary="在安全基线下丢弃 Workspace pending",
)
async def discard_pending_restart(
    payload: ConfigPendingDiscardRequest,
    candidate_ref: str = Query(..., min_length=1),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    try:
        config_service.discard_pending_restart(
            candidate_ref=candidate_ref,
            expected_active_revision=payload.expected_active_revision,
            expected_active_digest=payload.expected_active_digest,
        )
    except (ConfigConflictError, ValueError) as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=config_service.get_reload_status_dto(), request_id=request_id)


@router.patch("", response_model=APIResponse[ConfigDTO], summary="更新配置")
async def update_config(
    payload: ConfigUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    try:
        result = await config_service.update(payload)
    except ConfigConflictError as error:
        revision, _schema_path, sources = config_service.get_source_diagnostics()
        status = config_service.get_reload_status_dto()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conflict",
                "message": str(error),
                "current_revision": revision,
                "current_active_revision": status.active_revision,
                "current_active_digest": revision,
                "sources": [
                    {
                        "source_key": source.source_key,
                        "presence": source.presence,
                        "layer_revision": source.layer_revision,
                        "layer_digest": source.layer_digest,
                        "source_generation": source.source_generation,
                    }
                    for source in sources
                ],
            },
        ) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/events",
    response_model=APIResponse[ConfigEventsDTO],
    summary="重放配置变化事件",
)
async def get_config_events(
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=2000),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    config_service: ConfigService = Depends(get_config_service),
):
    try:
        events = config_service.list_config_events(after=after, limit=limit)
    except ConfigEventCursorGoneError as error:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "snapshot_required",
                "message": str(error),
                "first_available": error.first_available,
            },
        ) from error
    return APIResponse(
        data=ConfigEventsDTO(
            cursor=events[-1].event_seq if events else after,
            events=[
                ConfigEventDTO(
                    event_seq=event.event_seq,
                    event_id=event.event_id,
                    config_domain=event.config_domain,
                    candidate_id=event.candidate_id,
                    attempt_id=event.attempt_id,
                    apply_id=event.apply_id,
                    idempotency_key=event.idempotency_key,
                    commit_revision=event.commit_revision,
                    active_revision=event.active_revision,
                    pending_revision=event.pending_revision,
                    source=event.source,
                    result=event.result,
                    activation_scope=event.activation_scope,
                    changed_paths=list(event.changed_paths),
                    applied_paths=list(event.applied_paths),
                    deferred_paths=list(event.deferred_paths),
                    error=event.error,
                    occurred_at=event.occurred_at.isoformat(),
                )
                for event in events
            ],
            has_more=len(events) == limit,
        ),
        request_id=request_id,
    )


@router.get(
    "/events/stream",
    response_class=StreamingResponse,
    summary="订阅配置变化事件",
    responses=sse_responses("工作区配置事件与明确读取拒绝", {"config": ConfigEventDTO, "error": SseErrorDTO}),
)
async def stream_config_events(
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _: str = Depends(verify_local_token),
    config_service: ConfigService = Depends(get_config_service),
):
    if last_event_id is not None:
        try:
            header_cursor = int(last_event_id)
        except ValueError as error:
            raise HTTPException(
                status_code=400,
                detail="Last-Event-ID 必须是整数配置事件游标",
            ) from error
        if after and after != header_cursor:
            raise HTTPException(status_code=409, detail="after 与 Last-Event-ID 不一致")
        after = header_cursor
    try:
        config_service.ensure_config_event_cursor(after=after)
    except ConfigEventCursorGoneError as error:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "snapshot_required",
                "message": str(error),
                "first_available": error.first_available,
            },
        ) from error

    async def event_generator():
        cursor = after
        consumer_id = f"workspace-config-sse:{uuid4().hex}"
        while not await request.is_disconnected():
            # 空闲时不进 claim 写事务：先用只读探测确认确有待消费事件，再 claim。
            # 否则无事的连接也会每秒 BEGIN IMMEDIATE + sweep，白白占用写事务。
            # 若游标已被裁剪，探测会抛 CursorGone，此时按原有续读语义交给 claim 处理。
            try:
                has_pending = bool(
                    config_service.list_config_events(after=cursor, limit=1)
                )
            except ConfigEventCursorGoneError:
                has_pending = True
            events = (
                config_service.claim_config_events_for_consumer(
                    after=cursor,
                    consumer_id=consumer_id,
                    limit=2000,
                )
                if has_pending
                else ()
            )
            if events:
                for event in events:
                    cursor = event.event_seq
                    dto = ConfigEventDTO(
                        event_seq=event.event_seq,
                        event_id=event.event_id,
                        config_domain=event.config_domain,
                        candidate_id=event.candidate_id,
                        attempt_id=event.attempt_id,
                        apply_id=event.apply_id,
                        idempotency_key=event.idempotency_key,
                        commit_revision=event.commit_revision,
                        active_revision=event.active_revision,
                        pending_revision=event.pending_revision,
                        source=event.source,
                        result=event.result,
                        activation_scope=event.activation_scope,
                        changed_paths=list(event.changed_paths),
                        applied_paths=list(event.applied_paths),
                        deferred_paths=list(event.deferred_paths),
                        error=event.error,
                        occurred_at=event.occurred_at.isoformat(),
                    )
                    yield f"id: {cursor}\nevent: config\ndata: {dto.model_dump_json()}\n\n"
                    config_service.mark_config_event_delivered_for_consumer(
                        event_id=event.event_id,
                        consumer_id=consumer_id,
                    )
            else:
                yield ": config-heartbeat\n\n"
                await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_NO_CACHE_HEADERS,
    )
