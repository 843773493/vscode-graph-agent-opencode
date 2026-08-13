from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_node_debug_service, get_request_id, verify_local_token
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.node_debug import (
    NodeDebugActionRequest,
    NodeDebugCapabilitiesDTO,
    NodeDebugConfigurationActivateRequest,
    NodeDebugConfigurationCopyRequest,
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationImportRequest,
    NodeDebugConfigurationUpdateRequest,
    NodeDebugStartRequest,
    NodeDebugStateDTO,
)
from app.services.infrastructure.node_debug_service import NodeDebugService

router = APIRouter(prefix="/debug/node", tags=["node-debug"])


def _configuration_error(error: Exception) -> HTTPException:
    if isinstance(error, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (TypeError, ValueError)):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=409, detail=str(error))


@router.get(
    "/capabilities",
    response_model=APIResponse[NodeDebugCapabilitiesDTO],
    summary="获取源码调试能力和启动配置",
)
async def get_node_debug_capabilities(
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    result = node_debug_service.get_capabilities()
    return APIResponse(data=result, request_id=request_id)


@router.get("", response_model=APIResponse[NodeDebugStateDTO], summary="获取 Node 源码调试状态")
async def get_node_debug_state(
    session_id: Annotated[str, Query(min_length=1)],
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    result = await node_debug_service.get_state(session_id)
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/configurations",
    response_model=APIResponse[list[NodeDebugConfigurationDTO]],
    summary="列出当前会话的源码调试方案",
)
async def list_node_debug_configurations(
    session_id: Annotated[str, Query(min_length=1)],
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    result = node_debug_service.list_configurations(session_id)
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/configurations/{configuration_id}",
    response_model=APIResponse[NodeDebugConfigurationDTO],
    summary="导出单个可移植源码调试方案",
)
async def get_node_debug_configuration(
    configuration_id: str,
    session_id: Annotated[str, Query(min_length=1)],
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    try:
        result = node_debug_service.get_configuration(session_id, configuration_id)
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as error:
        raise _configuration_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/configurations",
    response_model=APIResponse[NodeDebugStateDTO],
    summary="创建源码调试方案",
)
async def create_node_debug_configuration(
    payload: NodeDebugConfigurationCreateRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    try:
        result = await node_debug_service.create_configuration(payload)
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as error:
        raise _configuration_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.put(
    "/configurations/{configuration_id}",
    response_model=APIResponse[NodeDebugStateDTO],
    summary="更新源码调试方案",
)
async def update_node_debug_configuration(
    configuration_id: str,
    payload: NodeDebugConfigurationUpdateRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    try:
        result = await node_debug_service.update_configuration(
            configuration_id,
            payload,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as error:
        raise _configuration_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/configurations/{configuration_id}/activate",
    response_model=APIResponse[NodeDebugStateDTO],
    summary="激活源码调试方案",
)
async def activate_node_debug_configuration(
    configuration_id: str,
    payload: NodeDebugConfigurationActivateRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    try:
        result = await node_debug_service.activate_configuration(
            payload.session_id,
            configuration_id,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as error:
        raise _configuration_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.delete(
    "/configurations/{configuration_id}",
    response_model=APIResponse[NodeDebugStateDTO],
    summary="删除源码调试方案",
)
async def delete_node_debug_configuration(
    configuration_id: str,
    session_id: Annotated[str, Query(min_length=1)],
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    try:
        result = await node_debug_service.delete_configuration(
            session_id,
            configuration_id,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as error:
        raise _configuration_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/configurations/import",
    response_model=APIResponse[NodeDebugStateDTO],
    summary="导入可移植源码调试方案",
)
async def import_node_debug_configuration(
    payload: NodeDebugConfigurationImportRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    try:
        result = await node_debug_service.import_configuration(
            payload.session_id,
            payload.configuration,
            activate=payload.activate,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as error:
        raise _configuration_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/configurations/{configuration_id}/copy",
    response_model=APIResponse[NodeDebugConfigurationDTO],
    summary="把源码调试方案复制到另一会话",
)
async def copy_node_debug_configuration(
    configuration_id: str,
    payload: NodeDebugConfigurationCopyRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    try:
        result = await node_debug_service.copy_configuration(
            source_session_id=payload.source_session_id,
            target_session_id=payload.target_session_id,
            configuration_id=configuration_id,
            name=payload.name,
            activate=payload.activate,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as error:
        raise _configuration_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/start", response_model=APIResponse[NodeDebugStateDTO], summary="启动 Node 源码调试")
async def start_node_debug(
    payload: NodeDebugStartRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    try:
        result = await node_debug_service.start(
            session_id=payload.session_id,
            configuration_id=payload.configuration_id,
            path=payload.path,
            args=payload.args,
            breakpoints=payload.breakpoints,
            launch_profile_name=payload.launch_profile_name,
            working_directory=payload.working_directory,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/action", response_model=APIResponse[NodeDebugStateDTO], summary="控制 Node 源码调试")
async def apply_node_debug_action(
    payload: NodeDebugActionRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    node_debug_service: Annotated[NodeDebugService, Depends(get_node_debug_service)],
):
    try:
        result = await node_debug_service.apply_action(
            session_id=payload.session_id,
            action=payload.action,
            params=payload.params,
        )
    except (TypeError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)
