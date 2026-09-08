from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_config_service, get_request_id, verify_local_token
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.config import (
    ConfigDTO,
    ConfigEventDTO,
    ConfigEventsDTO,
    ConfigPendingDiscardRequest,
    ConfigPendingHealthProofRequest,
    ConfigReloadStatusDTO,
    ConfigRestartFailureRequest,
    ConfigSourceDTO,
    ConfigSourcesDTO,
    ConfigStartupContractDTO,
    ConfigUpdateRequest,
)
from app.services.infrastructure.config.policy import workspace_config_policy
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventCursorGoneError,
)
from app.services.infrastructure.config_service import ConfigService

router = APIRouter(prefix="/config", tags=["config"])


def _reload_status_dto(config_service: ConfigService) -> ConfigReloadStatusDTO:
    status = config_service.get_reload_status()
    return ConfigReloadStatusDTO(
        healthy=status.healthy,
        revision=status.revision,
        restart_required=status.restart_required,
        reason=status.reason,
        changed_sections=list(status.changed_sections),
        last_success_at=status.last_success_at.isoformat(),
        last_attempt_at=status.last_attempt_at.isoformat(),
        last_error=status.last_error,
        state=status.state,
        active_revision=status.active_revision,
        pending_revision=status.pending_revision,
        candidate_id=status.candidate_id,
        candidate_ref=status.candidate_ref,
        attempt_id=status.attempt_id,
        apply_id=status.apply_id,
        layer_digests=status.layer_digests or {},
        applied_paths=list(status.applied_paths),
        deferred_paths=list(status.deferred_paths),
    )


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
        data=_reload_status_dto(config_service),
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
    revision, schema_path, sources = config_service.get_source_diagnostics()
    return APIResponse(
        data=ConfigSourcesDTO(
            revision=revision,
            schema_path=str(schema_path),
            sources=[
                ConfigSourceDTO(
                    path=str(source.path),
                    layer=source.layer,
                    precedence=source.precedence,
                    loaded=source.loaded,
                    source_key=source.source_key,
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
        raise HTTPException(status_code=409, detail=str(error)) from error
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
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=_reload_status_dto(config_service), request_id=request_id)


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
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=_reload_status_dto(config_service), request_id=request_id)


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
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=_reload_status_dto(config_service), request_id=request_id)


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
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=_reload_status_dto(config_service), request_id=request_id)


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
        revision, schema_path, sources = config_service.get_source_diagnostics()
        status = _reload_status_dto(config_service)
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
            events = config_service.claim_config_events_for_consumer(
                after=cursor,
                consumer_id=consumer_id,
                limit=2000,
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

    return StreamingResponse(event_generator(), media_type="text/event-stream")
