"""Gateway 健康检查与配置重载路由。"""

from __future__ import annotations

import asyncio
import os
from datetime import (
    datetime,
    timezone,
)
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import StreamingResponse

from app.api.sse_heartbeat import SSE_NO_CACHE_HEADERS
from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_access,
    verify_gateway_token,
)
from app.gateway.config import (
    GatewayConfigReloadService,
    load_gateway_config,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.control.scheduler import SessionGeneratorScheduler
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import (
    _gateway_root,
    get_gateway_config_reload_service,
    get_registry,
)
from app.gateway.runtime.development_restart import (
    RESTART_DELAY_MS,
    resolve_development_restart_command,
    start_development_restart,
)
from app.schemas.gateway import (
    DevelopmentRuntimeRestartDTO,
    GatewayConfigEventDTO,
    GatewayConfigEventsDTO,
    GatewayConfigPendingDiscardRequest,
    GatewayConfigPendingHealthProofRequest,
    GatewayConfigReloadStatusDTO,
    GatewayConfigSourceDTO,
    GatewayConfigSourcesDTO,
    GatewayHealthDTO,
)
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.config.policy import gateway_config_policy
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventCursorGoneError,
)
from app.services.infrastructure.config_service import (
    release_inline_config_vrn_for_file,
)

router = APIRouter()


def _gateway_config_event_dto(event) -> GatewayConfigEventDTO:
    return GatewayConfigEventDTO(
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


@router.get("/api/gateway/health", response_model=APIResponse[GatewayHealthDTO])
async def health(
    request: Request,
    request_id: str = Depends(get_request_id),
    registry: GatewayWorkspaceRegistry = Depends(get_registry),
):
    scheduler = getattr(request.app.state, "session_generator_scheduler", None)
    if not isinstance(scheduler, SessionGeneratorScheduler):
        raise RuntimeError("会话生成器调度器尚未初始化")
    scheduler.assert_healthy()
    return APIResponse(
        data=GatewayHealthDTO(
            active_workspace_id=registry.active_workspace_id,
            process_id=os.getpid(),
            development_restart_available=(
                resolve_development_restart_command() is not None
            ),
        ),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/runtime/restart-development",
    response_model=APIResponse[DevelopmentRuntimeRestartDTO],
)
async def restart_development_runtime(
    request: Request,
    auth: GatewayAuthContext = Depends(verify_gateway_access),
    request_id: str = Depends(get_request_id),
):
    if auth.kind != "local":
        raise HTTPException(status_code=403, detail="远程 Gateway 无权重启本机开发服务")
    command = resolve_development_restart_command()
    if command is None:
        raise HTTPException(status_code=409, detail="当前不是可重启的源码开发环境")
    restart_environment: dict[str, str] = {}
    gateway_state = getattr(request.app.state, "gateway_state", None)
    gateway_reload = getattr(request.app.state, "gateway_config_reload", None)
    if isinstance(gateway_state, GatewayStateStore) and isinstance(
        gateway_reload, GatewayConfigReloadService
    ):
        candidate_ref = gateway_reload.status().candidate_ref
        if candidate_ref:
            intent = gateway_state.get_gateway_restart_intent(
                candidate_ref=candidate_ref
            )
            if intent is None or intent.state not in {"pending", "applying"}:
                raise HTTPException(
                    status_code=409,
                    detail="Gateway pending restart intent 已不可恢复",
                )
            if intent.expires_at is None or intent.expires_at <= datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=409,
                    detail="Gateway pending restart intent 已过期，请先显式 retry",
                )
            restart_environment = {
                "BOXTEAM_CONFIG_CANDIDATE_REF": intent.candidate_ref,
                "BOXTEAM_CONFIG_GENERATION": intent.target_generation,
                "BOXTEAM_CONFIG_FENCING_TOKEN": intent.fencing_token,
            }
    helper_process_id = start_development_restart(
        command,
        log_path=_gateway_root() / "logs" / "development-restart.log",
        environment=restart_environment,
    )
    return APIResponse(
        data=DevelopmentRuntimeRestartDTO(
            previous_process_id=os.getpid(),
            helper_process_id=helper_process_id,
            delay_ms=RESTART_DELAY_MS,
        ),
        request_id=request_id,
    )


@router.get(
    "/api/gateway/config/sources",
    response_model=APIResponse[GatewayConfigSourcesDTO],
)
async def gateway_config_sources(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    try:
        gateway_state = getattr(request.app.state, "gateway_state", None)
        config = (
            load_gateway_config(
                state_store=gateway_state,
                persist_migrations=False,
            )
            if isinstance(gateway_state, GatewayStateStore)
            else load_gateway_config()
        )
    except (FileNotFoundError, TypeError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if config.schema_path is None:
        raise RuntimeError("Gateway 配置快照缺少 schema 来源")
    return APIResponse(
        data=GatewayConfigSourcesDTO(
            revision=config.revision,
            # TODO(5A.3): 字段名与 proto 的 GatewayConfigSourcesDTO.schema_path 属独立
            # 破坏性协议切片，本切片不改其名；值改为 config kind 的来源 VRN（可为空）。
            # 仅当生效 schema 与发行包 gateway_schema.jsonc 内容一致时才编 inline VRN，
            # 否则返回空串（非 inline schema 不得编造来源身份）。
            schema_path=release_inline_config_vrn_for_file(
                config.schema_path,
                release_config_name="gateway_schema.jsonc",
            )
            or "",
            sources=[
                GatewayConfigSourceDTO(
                    # TODO(5A.3): 同 app/api/config.py，字段名属独立破坏性协议切片，
                    # 本切片只把值由 real path 改为来源 VRN（可为空）。
                    path=source.vrn or "",
                    layer=source.layer,
                    precedence=source.precedence,
                    loaded=source.loaded,
                    source_key=source.source_key,
                    presence=source.presence,
                    layer_revision=source.layer_revision,
                    layer_digest=source.layer_digest,
                    source_generation=source.source_generation,
                )
                for source in config.source_details
            ],
            policy_manifest=list(gateway_config_policy().policy_manifest()),
        ),
        request_id=request_id,
    )


@router.get(
    "/api/gateway/config/reload-status",
    response_model=APIResponse[GatewayConfigReloadStatusDTO],
)
async def gateway_config_reload_status(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    status = get_gateway_config_reload_service(request).status()
    return APIResponse(
        data=GatewayConfigReloadStatusDTO(
            available=True,
            healthy=status.healthy,
            revision=status.revision,
            restart_required=status.restart_required,
            reason=status.reason,
            changed_sections=list(status.changed_sections),
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
        ),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/config/retry-restart",
    response_model=APIResponse[GatewayConfigReloadStatusDTO],
)
async def retry_gateway_config_restart(
    request: Request,
    candidate_ref: str = Query(..., min_length=1),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    try:
        status = get_gateway_config_reload_service(request).retry_pending_restart(
            candidate_ref=candidate_ref,
            requested_by=f"gateway-api:{request_id}",
        )
    except (ConfigConflictError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=GatewayConfigReloadStatusDTO(
            available=True,
            healthy=status.healthy,
            revision=status.revision,
            restart_required=status.restart_required,
            reason=status.reason,
            changed_sections=list(status.changed_sections),
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
        ),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/config/resolve-restart",
    response_model=APIResponse[GatewayConfigReloadStatusDTO],
)
async def resolve_gateway_config_restart(
    request: Request,
    payload: GatewayConfigPendingHealthProofRequest,
    candidate_ref: str = Query(..., min_length=1),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    try:
        status = get_gateway_config_reload_service(request).resolve_pending_restart(
            candidate_ref=candidate_ref,
            health_proof=payload.model_dump(exclude_none=True),
        )
    except (ConfigConflictError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=GatewayConfigReloadStatusDTO(
            available=True,
            healthy=status.healthy,
            revision=status.revision,
            restart_required=status.restart_required,
            reason=status.reason,
            changed_sections=list(status.changed_sections),
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
        ),
        request_id=request_id,
    )


@router.post(
    "/api/gateway/config/discard-restart",
    response_model=APIResponse[GatewayConfigReloadStatusDTO],
)
async def discard_gateway_config_restart(
    request: Request,
    payload: GatewayConfigPendingDiscardRequest,
    candidate_ref: str = Query(..., min_length=1),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    try:
        status = get_gateway_config_reload_service(request).discard_pending_restart(
            candidate_ref=candidate_ref,
            expected_active_revision=payload.expected_active_revision,
            expected_active_digest=payload.expected_active_digest,
        )
    except (ConfigConflictError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(
        data=GatewayConfigReloadStatusDTO(
            available=True,
            healthy=status.healthy,
            revision=status.revision,
            restart_required=status.restart_required,
            reason=status.reason,
            changed_sections=list(status.changed_sections),
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
        ),
        request_id=request_id,
    )


@router.get(
    "/api/gateway/config/events",
    response_model=APIResponse[GatewayConfigEventsDTO],
)
async def gateway_config_events(
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=2000),
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    service = get_gateway_config_reload_service(request)
    try:
        events = service.list_events(after=after, limit=limit)
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
        data=GatewayConfigEventsDTO(
            cursor=events[-1].event_seq if events else after,
            events=[_gateway_config_event_dto(event) for event in events],
            has_more=len(events) == limit,
        ),
        request_id=request_id,
    )


@router.get(
    "/api/gateway/config/events/stream",
    response_class=StreamingResponse,
)
async def gateway_config_event_stream(
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _: str = Depends(verify_gateway_token),
):
    if last_event_id is not None:
        try:
            header_cursor = int(last_event_id)
        except ValueError as error:
            raise HTTPException(
                status_code=400, detail="Last-Event-ID 必须是整数游标"
            ) from error
        if after and after != header_cursor:
            raise HTTPException(status_code=409, detail="after 与 Last-Event-ID 不一致")
        after = header_cursor
    service = get_gateway_config_reload_service(request)
    try:
        service.ensure_event_cursor(after=after)
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
        consumer_id = f"gateway-config-sse:{uuid4().hex}"
        while not await request.is_disconnected():
            # 空闲时不进 claim 写事务：先只读探测确有待消费事件，再 claim；
            # 否则无事的连接也会每秒 BEGIN IMMEDIATE + sweep，白白占用写事务。
            # 游标被裁剪时按原有续读语义交给 claim 处理。
            try:
                has_pending = bool(service.list_events(after=cursor, limit=1))
            except ConfigEventCursorGoneError:
                has_pending = True
            events = (
                service.claim_events_for_consumer(
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
                    dto = _gateway_config_event_dto(event)
                    yield f"id: {cursor}\nevent: config\ndata: {dto.model_dump_json()}\n\n"
                    service.mark_event_delivered_for_consumer(
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
