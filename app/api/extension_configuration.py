"""宿主配置的私有控制入口；资源只能来自已认证代际的冻结映射。"""

from __future__ import annotations

from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import StreamingResponse

from app.api.extension_host import host_registry
from app.api.sse_heartbeat import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    SSE_NO_CACHE_HEADERS,
    stream_sse_with_heartbeat,
)
from app.container import WorkspaceContainerRegistry
from app.core.trace_middleware import get_request_id
from app.schemas.gateway.extension_configuration import (
    ExtensionConfigurationEventDTO,
    ExtensionConfigurationFileRequest,
    ExtensionConfigurationReadDTO,
    ExtensionConfigurationStatDTO,
    ExtensionConfigurationViewRequest,
    ExtensionConfigurationWriteRequest,
)
from app.schemas.internal_v2.common import APIResponse
from app.services.infrastructure.extension_host.configuration import (
    ExtensionConfigurationFiles,
)
from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
)

router = APIRouter()


def configuration_files(request: Request, payload: ExtensionConfigurationViewRequest,
                        registry: WorkspaceContainerRegistry) -> ExtensionConfigurationFiles:
    if request.headers.get("x-boxteam-gateway-id") != payload.gateway_id:
        raise HTTPException(400, "配置请求的目标 Gateway 身份冲突")
    client_id = request.headers.get("x-boxteam-extension-client-id")
    if not client_id:
        raise HTTPException(400, "配置请求缺少 Gateway 认证的客户端绑定")
    try:
        return registry.extension_hosts.configuration_files(payload, client_id=client_id)
    except (ValueError, KeyError) as error:
        raise HTTPException(409, str(error)) from error


def configuration_error(error: ExtensionManagementError) -> HTTPException:
    status = 404 if error.code == "EntryNotFound" else 403 if error.code == "NoPermissions" else 409 if error.code == "EntryExists" else 400
    return HTTPException(status, detail={"owner": "extension_configuration", "code": error.code, "message": str(error)})


@router.post("/api/internal/extension-configurations/stat", response_model=APIResponse[ExtensionConfigurationStatDTO], include_in_schema=False)
async def configuration_stat(payload: ExtensionConfigurationFileRequest, request: Request,
    registry: Annotated[WorkspaceContainerRegistry, Depends(host_registry)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionConfigurationStatDTO]:
    files = configuration_files(request, payload, registry)
    try:
        result = await files.stat(payload.resource_uri)
    except ExtensionManagementError as error:
        raise configuration_error(error) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    configuration_files(request, payload, registry)
    return APIResponse(data=result, request_id=request_id)


@router.post("/api/internal/extension-configurations/read", response_model=APIResponse[ExtensionConfigurationReadDTO], include_in_schema=False)
async def configuration_read(payload: ExtensionConfigurationFileRequest, request: Request,
    registry: Annotated[WorkspaceContainerRegistry, Depends(host_registry)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionConfigurationReadDTO]:
    files = configuration_files(request, payload, registry)
    try:
        result = await files.read(payload.resource_uri)
    except ExtensionManagementError as error:
        raise configuration_error(error) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    configuration_files(request, payload, registry)
    return APIResponse(data=result, request_id=request_id)


@router.post("/api/internal/extension-configurations/write", response_model=APIResponse[ExtensionConfigurationStatDTO], include_in_schema=False)
async def configuration_write(payload: ExtensionConfigurationWriteRequest, request: Request,
    registry: Annotated[WorkspaceContainerRegistry, Depends(host_registry)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> APIResponse[ExtensionConfigurationStatDTO]:
    files = configuration_files(request, payload, registry)
    try:
        result = await files.write(payload.resource_uri, payload.content_base64, payload.options)
    except ExtensionManagementError as error:
        raise configuration_error(error) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    configuration_files(request, payload, registry)
    return APIResponse(data=result, request_id=request_id)


@router.post("/api/internal/extension-configurations/events", include_in_schema=False)
async def configuration_events(payload: ExtensionConfigurationViewRequest, request: Request,
    registry: Annotated[WorkspaceContainerRegistry, Depends(host_registry)],
) -> StreamingResponse:
    files = configuration_files(request, payload, registry)

    async def generate():
        first = True
        async with aclosing(stream_sse_with_heartbeat(files.events(), heartbeat_interval_seconds=SSE_HEARTBEAT_INTERVAL_SECONDS,
                            close_source_on_exit=True, is_disconnected=request.is_disconnected)) as stream:
            async for batch in stream:
                try:
                    configuration_files(request, payload, registry)
                except HTTPException:
                    event = ExtensionConfigurationEventDTO(changes=[], overflow=False, error="配置所属宿主代际已撤销")
                    yield f"event: error\ndata: {event.model_dump_json()}\n\n"
                    return
                if isinstance(batch, str):
                    yield batch
                    continue
                event_name = "error" if batch.error is not None else "ready" if first else "changes"
                first = False
                yield f"event: {event_name}\ndata: {batch.model_dump_json()}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream", headers=SSE_NO_CACHE_HEADERS)
