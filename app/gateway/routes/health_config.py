"""Gateway 健康检查与配置重载路由。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import (
    UTC,
    datetime,
)
from typing import Annotated, Literal
from urllib.parse import quote, urlencode
from uuid import uuid4

import httpx
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from app.api.sse_heartbeat import SSE_NO_CACHE_HEADERS
from app.core.failures import failure_detail
from app.core.logging_contract import get_runtime_log_root
from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import get_request_id
from app.gateway.auth import (
    GatewayAuthContext,
    verify_gateway_access,
)
from app.gateway.config import (
    GatewayConfigReloadService,
    load_gateway_config,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation import send_remote_gateway_management
from app.gateway.federation.policy import (
    federation_capability_allowed,
    require_federation_capability,
)
from app.gateway.proxy_upstream import (
    TARGET_GATEWAY_ID_HEADER,
    UpstreamResponseBody,
    parse_target_gateway_id,
)
from app.gateway.registry import GatewayWorkspaceRegistry
from app.gateway.routes._shared import (
    get_gateway_config_reload_service,
    get_registry,
)
from app.gateway.runtime.development_restart import (
    RESTART_DELAY_MS,
    resolve_development_restart_command,
    start_development_restart,
)
from app.schemas.gateway.config import (
    GatewayConfigEventDTO,
    GatewayConfigEventsDTO,
    GatewayConfigPendingDiscardRequest,
    GatewayConfigPendingHealthProofRequest,
    GatewayConfigReloadStatusDTO,
    GatewayConfigSourceDocumentDTO,
    GatewayConfigSourceDTO,
    GatewayConfigSourceEditRequest,
    GatewayConfigSourcePreflightDTO,
    GatewayConfigSourcesDTO,
)
from app.schemas.gateway.diagnostics import (
    DevelopmentRuntimeRestartDTO,
    GatewayHealthDTO,
)
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.sse import SseErrorDTO, sse_responses
from app.services.infrastructure.config.policy import gateway_config_policy
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigEventCursorGoneError,
)
from app.services.infrastructure.config_service import (
    release_inline_config_uri_for_file,
)

router = APIRouter()


@dataclass(frozen=True, slots=True)
class _GatewayConfigTarget:
    gateway_id: str
    connection_id: str | None


def _gateway_config_target(
    request: Request,
    target_gateway_id: Annotated[str | None, Header(alias=TARGET_GATEWAY_ID_HEADER)] = None,
    gateway_connection_id: str | None = Query(default=None, min_length=1),
) -> _GatewayConfigTarget | None:
    target = parse_target_gateway_id(target_gateway_id)
    if gateway_connection_id is not None and target is None:
        raise HTTPException(status_code=400, detail="gateway_connection_id 必须同时指定稳定目标 Gateway")
    if target is None:
        return None
    if target == request.app.state.gateway_id:
        if gateway_connection_id is not None:
            raise HTTPException(status_code=409, detail="本机 Gateway 目标不能指定远端连接")
        return None
    return _GatewayConfigTarget(gateway_id=target, connection_id=gateway_connection_id)


def _require_native_gateway_config_target(
    target: Annotated[_GatewayConfigTarget | None, Depends(_gateway_config_target)],
) -> None:
    if target is not None:
        raise HTTPException(status_code=409, detail="此配置操作只支持当前原生 Gateway，尚未提供远端 Relay")


class _GatewayConfigStreamResponse(StreamingResponse):
    def __init__(self, body: UpstreamResponseBody, *, headers: dict[str, str]) -> None:
        self._relay_body = body
        super().__init__(body, media_type="text/event-stream", headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # 响应头发送失败时 body 尚未开始，仍须等同一次关闭完成后才释放连接引用。
            await self._relay_body.aclose()


async def _relay_gateway_config(
    request: Request,
    *,
    auth: GatewayAuthContext,
    target: _GatewayConfigTarget | None,
    method: Literal["GET", "POST", "PUT"],
    endpoint: str,
    request_id: str,
    response_model: type[BaseModel] = GatewayConfigSourcesDTO,
    source_key: str | None = None,
    stream: bool = False,
) -> Response | None:
    if target is None:
        return None
    if auth.kind != "local":
        raise HTTPException(status_code=403, detail="联邦入站请求不能借本机凭据再次 Relay 到其它 Gateway")
    registry = get_registry(request)
    connection_id = target.connection_id
    if connection_id is None:
        matches = [connection for connection in registry.remote_gateway_connections()
                   if connection.remote_gateway_id == target.gateway_id]
        if not matches:
            raise HTTPException(status_code=404, detail="目标 Gateway 未登记远端连接")
        if len(matches) != 1:
            raise HTTPException(status_code=409, detail="目标 Gateway 存在多条连接，请明确指定 gateway_connection_id")
        connection_id = matches[0].connection_id
    try:
        connection = registry.remote_gateway_connection(connection_id)
        if connection.remote_gateway_id != target.gateway_id:
            raise HTTPException(status_code=409, detail="远端连接与稳定目标 Gateway 归属不匹配")
        credential = FederationCredentialStore(
            storage_path=get_gateway_root() / "credentials" / "federation.json"
        ).get(connection_id)
        gateway_url = registry.acquire_remote_gateway_reference(connection_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (PermissionError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    response: httpx.Response | None = None
    body: UpstreamResponseBody | None = None
    handed_off = False
    # 在首次 await 前冻结连接、凭据和 URL；换代、拒绝或超时均不重选连接、重放请求。
    try:
        path = "/api/gateway/config/" + endpoint
        query = urlencode([(key, value) for key, value in request.query_params.multi_items()
                           if key != "gateway_connection_id"])
        if query:
            path += "?" + query
        response = await send_remote_gateway_management(
            gateway_url=gateway_url,
            credential=credential,
            method=method,
            path=path,
            request_id=request_id,
            content=await request.body(),
            content_type=request.headers.get("content-type"),
            target_gateway_id=target.gateway_id,
            sender_gateway_id=registry.gateway_id,
            last_event_id=request.headers.get("last-event-id") if stream else None,
            stream=stream,
        )
        body = UpstreamResponseBody(response, on_close=lambda: registry.release_remote_gateway_reference(connection_id))
        headers = {name: response.headers[name] for name in ("content-type", "x-request-id", "cache-control", "x-accel-buffering")
                   if name in response.headers}
        if stream and response.is_success:
            if (response.headers.get("x-request-id") != request_id
                    or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream"):
                raise HTTPException(status_code=502, detail="远端配置事件流的 request_id 或媒体类型不匹配")
            result = _GatewayConfigStreamResponse(body, headers=headers)
            handed_off = True
            return result
        if stream:
            await response.aread()
        if response.is_success:
            try:
                envelope = response.json()
                if not isinstance(envelope, dict) or envelope.get("request_id") != request_id:
                    raise ValueError("远端来源响应 request_id 不匹配")
                data = response_model.model_validate(envelope.get("data"))
                document = data.document if isinstance(data, GatewayConfigSourcePreflightDTO) else data
                if isinstance(document, GatewayConfigSourceDocumentDTO) and (
                    document.gateway_id, document.source_key
                ) != (target.gateway_id, source_key):
                    raise ValueError("远端来源响应归属不匹配")
            except (TypeError, ValueError) as error:
                raise HTTPException(status_code=502, detail="远端配置成功响应的归属或协议不匹配") from error
        return Response(content=response.content, status_code=response.status_code, headers=headers)
    except (httpx.RequestError, TimeoutError) as error:
        raise HTTPException(status_code=502, detail=failure_detail(
            error, stage="gateway-config-relay",
            label=("远端来源请求传输失败，请用原幂等键查询结果" if source_key is not None
                   else "远端配置请求传输失败，请查询原候选或游标，不自动重放"),
        )) from error
    finally:
        if not handed_off:
            if body is not None:
                await body.aclose()
            else:
                registry.release_remote_gateway_reference(connection_id)


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
    request.app.state.federation_channel_owner.assert_healthy()
    extension_management = request.app.state.extension_management
    if extension_management is not None:
        extension_management.assert_healthy()
    return APIResponse(
        data=GatewayHealthDTO(
            gateway_id=request.app.state.gateway_id,
            active_workspace=registry.active_workspace_dto,
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
    dependencies=[Depends(_require_native_gateway_config_target)],
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
            if intent.expires_at is None or intent.expires_at <= datetime.now(UTC):
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
        log_dir=get_runtime_log_root(),
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
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    target: Annotated[_GatewayConfigTarget | None, Depends(_gateway_config_target)],
    request_id: str = Depends(get_request_id),
):
    relayed = await _relay_gateway_config(request, auth=auth, target=target, method="GET", endpoint="sources", request_id=request_id)
    if relayed is not None:
        return relayed
    require_federation_capability(request, auth, "configuration_read")
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
    require_federation_capability(request, auth, "configuration_read")
    return APIResponse(
        data=GatewayConfigSourcesDTO(
            revision=config.revision,
            schema_uri=release_inline_config_uri_for_file(
                config.schema_path,
                release_config_name="gateway_schema.jsonc",
            ),
            sources=[
                GatewayConfigSourceDTO(
                    uri=source.uri,
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
    "/api/gateway/config/sources/{source_key}",
    response_model=APIResponse[GatewayConfigSourceDocumentDTO],
)
async def gateway_config_source_document(
    request: Request,
    source_key: str,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    service: Annotated[GatewayConfigReloadService, Depends(get_gateway_config_reload_service)],
    target: Annotated[_GatewayConfigTarget | None, Depends(_gateway_config_target)],
    idempotency_key: str | None = Query(default=None),
    request_id: str = Depends(get_request_id),
):
    relayed = await _relay_gateway_config(
        request, auth=auth, target=target, method="GET", endpoint=f"sources/{quote(source_key, safe='')}",
        request_id=request_id, source_key=source_key, response_model=GatewayConfigSourceDocumentDTO,
    )
    if relayed is not None:
        return relayed
    require_federation_capability(request, auth, "configuration_read")
    try:
        result = service.get_source_document(source_key=source_key, idempotency_key=idempotency_key)
    except ConfigConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    require_federation_capability(request, auth, "configuration_read")
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/api/gateway/config/sources/{source_key}/preflight",
    response_model=APIResponse[GatewayConfigSourcePreflightDTO],
)
async def preflight_gateway_config_source(
    request: Request,
    source_key: str,
    payload: GatewayConfigSourceEditRequest,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    service: Annotated[GatewayConfigReloadService, Depends(get_gateway_config_reload_service)],
    target: Annotated[_GatewayConfigTarget | None, Depends(_gateway_config_target)],
    request_id: str = Depends(get_request_id),
):
    relayed = await _relay_gateway_config(
        request, auth=auth, target=target, method="POST", endpoint=f"sources/{quote(source_key, safe='')}/preflight",
        request_id=request_id, source_key=source_key, response_model=GatewayConfigSourcePreflightDTO,
    )
    if relayed is not None:
        return relayed
    require_federation_capability(request, auth, "configuration_read")
    require_federation_capability(request, auth, "configuration_write")
    try:
        result = service.preflight_source_edit(source_key=source_key, request=payload)
        if result.candidate.get("plugins") != service.config.payload.get("plugins"):
            request.app.state.service_plugins.validate(result.candidate)
    except ConfigConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    require_federation_capability(request, auth, "configuration_read")
    return APIResponse(data=result, request_id=request_id)


@router.put(
    "/api/gateway/config/sources/{source_key}",
    response_model=APIResponse[GatewayConfigSourceDocumentDTO],
)
async def save_gateway_config_source(
    request: Request,
    source_key: str,
    payload: GatewayConfigSourceEditRequest,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    service: Annotated[GatewayConfigReloadService, Depends(get_gateway_config_reload_service)],
    target: Annotated[_GatewayConfigTarget | None, Depends(_gateway_config_target)],
    request_id: str = Depends(get_request_id),
):
    relayed = await _relay_gateway_config(
        request, auth=auth, target=target, method="PUT", endpoint=f"sources/{quote(source_key, safe='')}",
        request_id=request_id, source_key=source_key, response_model=GatewayConfigSourceDocumentDTO,
    )
    if relayed is not None:
        return relayed
    plugin_binding_changed = False

    def require_write() -> None:
        require_federation_capability(request, auth, "configuration_read")
        require_federation_capability(request, auth, "configuration_write")
        if plugin_binding_changed:
            require_federation_capability(request, auth, "service_plugin_runtime_manage")

    require_write()
    try:
        from app.gateway.runtime.service_plugins.runtime import (
            gateway_plugin_bindings,
            workspace_default_bindings,
        )
        from configs.plugin_parameters import plugin_parameters

        def authorize_plugins(candidate) -> None:
            nonlocal plugin_binding_changed
            plugin_binding_changed = (
                gateway_plugin_bindings(candidate.payload) != gateway_plugin_bindings(service.config.payload)
                or workspace_default_bindings(candidate.payload) != workspace_default_bindings(service.config.payload)
                or plugin_parameters(candidate.payload) != plugin_parameters(service.config.payload)
            )
            if plugin_binding_changed:
                require_federation_capability(request, auth, "service_plugin_runtime_manage")
            if candidate.payload.get("plugins") != service.config.payload.get("plugins"):
                request.app.state.service_plugins.validate(candidate.payload)

        result = await service.save_source_edit(source_key=source_key, request=payload,
            before_write=require_write, authorize_candidate=authorize_plugins)
    except ConfigConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (TypeError, ValueError, FileNotFoundError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    require_federation_capability(request, auth, "configuration_read")
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/api/gateway/config/reload-status",
    response_model=APIResponse[GatewayConfigReloadStatusDTO],
)
async def gateway_config_reload_status(
    request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    target: Annotated[_GatewayConfigTarget | None, Depends(_gateway_config_target)],
    request_id: str = Depends(get_request_id),
):
    relayed = await _relay_gateway_config(
        request, auth=auth, target=target, method="GET", endpoint="reload-status",
        request_id=request_id, response_model=GatewayConfigReloadStatusDTO,
    )
    if relayed is not None:
        return relayed
    require_federation_capability(request, auth, "configuration_read")
    status = get_gateway_config_reload_service(request).get_reload_status_dto()
    require_federation_capability(request, auth, "configuration_read")
    return APIResponse(
        data=status,
        request_id=request_id,
    )


@router.post(
    "/api/gateway/config/retry-restart",
    response_model=APIResponse[GatewayConfigReloadStatusDTO],
)
async def retry_gateway_config_restart(
    request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    target: Annotated[_GatewayConfigTarget | None, Depends(_gateway_config_target)],
    candidate_ref: str = Query(..., min_length=1),
    request_id: str = Depends(get_request_id),
):
    relayed = await _relay_gateway_config(
        request, auth=auth, target=target, method="POST", endpoint="retry-restart",
        request_id=request_id, response_model=GatewayConfigReloadStatusDTO,
    )
    if relayed is not None:
        return relayed
    require_federation_capability(request, auth, "configuration_write")
    require_federation_capability(request, auth, "configuration_read")
    try:
        status = get_gateway_config_reload_service(request).retry_pending_restart(
            candidate_ref=candidate_ref,
            requested_by=f"gateway-api:{request_id}",
        )
    except (ConfigConflictError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    require_federation_capability(request, auth, "configuration_read")
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
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    target: Annotated[_GatewayConfigTarget | None, Depends(_gateway_config_target)],
    candidate_ref: str = Query(..., min_length=1),
    request_id: str = Depends(get_request_id),
):
    relayed = await _relay_gateway_config(
        request, auth=auth, target=target, method="POST", endpoint="resolve-restart",
        request_id=request_id, response_model=GatewayConfigReloadStatusDTO,
    )
    if relayed is not None:
        return relayed
    require_federation_capability(request, auth, "configuration_write")
    require_federation_capability(request, auth, "configuration_read")
    try:
        status = get_gateway_config_reload_service(request).resolve_pending_restart(
            candidate_ref=candidate_ref,
            health_proof=payload.model_dump(exclude_none=True),
        )
    except (ConfigConflictError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    require_federation_capability(request, auth, "configuration_read")
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
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    target: Annotated[_GatewayConfigTarget | None, Depends(_gateway_config_target)],
    candidate_ref: str = Query(..., min_length=1),
    request_id: str = Depends(get_request_id),
):
    relayed = await _relay_gateway_config(
        request, auth=auth, target=target, method="POST", endpoint="discard-restart",
        request_id=request_id, response_model=GatewayConfigReloadStatusDTO,
    )
    if relayed is not None:
        return relayed
    require_federation_capability(request, auth, "configuration_write")
    require_federation_capability(request, auth, "configuration_read")
    try:
        status = get_gateway_config_reload_service(request).discard_pending_restart(
            candidate_ref=candidate_ref,
            expected_active_revision=payload.expected_active_revision,
            expected_active_digest=payload.expected_active_digest,
        )
    except (ConfigConflictError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    require_federation_capability(request, auth, "configuration_read")
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
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    target: Annotated[_GatewayConfigTarget | None, Depends(_gateway_config_target)],
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=2000),
    request_id: str = Depends(get_request_id),
):
    relayed = await _relay_gateway_config(
        request, auth=auth, target=target, method="GET", endpoint="events",
        request_id=request_id, response_model=GatewayConfigEventsDTO,
    )
    if relayed is not None:
        return relayed
    require_federation_capability(request, auth, "configuration_read")
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
    require_federation_capability(request, auth, "configuration_read")
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
    responses=sse_responses(
        "SSE Gateway 配置事件流",
        {"config": GatewayConfigEventDTO, "error": SseErrorDTO},
        models={"GatewayConfigEventDTO": GatewayConfigEventDTO, "SseErrorDTO": SseErrorDTO},
    ),
)
async def gateway_config_event_stream(
    request: Request,
    auth: Annotated[GatewayAuthContext, Depends(verify_gateway_access)],
    target: Annotated[_GatewayConfigTarget | None, Depends(_gateway_config_target)],
    request_id: Annotated[str, Depends(get_request_id)],
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    relayed = await _relay_gateway_config(
        request, auth=auth, target=target, method="GET", endpoint="events/stream", request_id=request_id, stream=True,
    )
    if relayed is not None:
        return relayed
    require_federation_capability(request, auth, "configuration_read")
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
            if not federation_capability_allowed(request, auth, "configuration_read"):
                error = SseErrorDTO(message="目标 Gateway 已撤销 configuration_read")
                yield f"event: error\ndata: {error.model_dump_json()}\n\n"
                return
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
                    dto = _gateway_config_event_dto(event)
                    frame = f"id: {event.event_seq}\nevent: config\ndata: {dto.model_dump_json()}\n\n"
                    if not federation_capability_allowed(request, auth, "configuration_read"):
                        error = SseErrorDTO(message="目标 Gateway 已撤销 configuration_read")
                        yield f"event: error\ndata: {error.model_dump_json()}\n\n"
                        return
                    cursor = event.event_seq
                    yield frame
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
