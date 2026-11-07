from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import ValidationError

from app.api.deps import (
    get_config_service,
    get_mcp_catalog_owner,
    get_mcp_connection_service,
    get_mcp_installation_target,
    get_request_id,
    verify_local_token,
)
from app.api.mcp_installation import mcp_candidate_errors
from app.core.trace_middleware import get_current_gateway_id
from app.schemas.internal_v2.mcp import (
    McpAuthorizationCallbackDTO,
    McpAuthorizationResponseDTO,
    McpCandidateConfigurationDraftRequestDTO,
    McpCandidateConfigurationDraftResponseDTO,
    McpCandidateConfigurationRequestDTO,
    McpCandidateConfigurationResponseDTO,
    McpCandidatePlanRequestDTO,
    McpCandidatePlanResponseDTO,
    McpConnectionOperationListResponseDTO,
    McpConnectionOperationResponseDTO,
    McpConnectionRequestDTO,
    McpInstallationTargetDTO,
    McpPrivateEnvWriteRequestDTO,
    McpPrivateEnvWriteResponseDTO,
    McpPrivateHeaderWriteRequestDTO,
    McpPrivateHeaderWriteResponseDTO,
    McpServerDTO,
    McpServerListResponseDTO,
    McpToolDTO,
    McpWorkspaceTargetDTO,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogRelistError
from app.services.infrastructure.mcp.connection_service import McpConnectionService

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _target(service: McpConnectionService) -> McpWorkspaceTargetDTO:
    return McpWorkspaceTargetDTO(gateway_id=get_current_gateway_id(), workspace_id=service.workspace_id)


@router.post("/private-headers", response_model=McpPrivateHeaderWriteResponseDTO, summary="向精确工作区写入单个私有 HTTP header，返回不可变引用")
async def write_mcp_private_header(
    payload: McpPrivateHeaderWriteRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    target = _target(service)
    with mcp_candidate_errors():
        data = await owner.write_private_header(payload, actual_target=target)
    return McpPrivateHeaderWriteResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/private-env", response_model=McpPrivateEnvWriteResponseDTO, summary="写入手动 stdio 定义的私有 env，返回不可变引用")
async def write_mcp_private_env(
    payload: McpPrivateEnvWriteRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    target = _target(service)
    with mcp_candidate_errors():
        data = await owner.write_private_env(payload, actual_target=target, installation_target=None, candidate_required=False)
    return McpPrivateEnvWriteResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/candidate-plans/private-env", response_model=McpPrivateEnvWriteResponseDTO, summary="重读原目录定义并写入其声明的私有 env")
async def write_mcp_candidate_private_env(
    payload: McpPrivateEnvWriteRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
    installation_target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    target = _target(service)
    with mcp_candidate_errors():
        data = await owner.write_private_env(payload, actual_target=target, installation_target=installation_target, candidate_required=True)
    return McpPrivateEnvWriteResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/candidate-configurations/draft", response_model=McpCandidateConfigurationDraftResponseDTO, summary="由原候选生成脱敏 JSONC 配置提案并无副作用预检")
async def draft_mcp_candidate_configuration(
    payload: McpCandidateConfigurationDraftRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
    installation_target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    config_service: Annotated[ConfigService, Depends(get_config_service)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    target = _target(service)
    with mcp_candidate_errors():
        data = await owner.draft_candidate_configuration(payload, actual_target=target,
            installation_target=installation_target, config_service=config_service)
    return McpCandidateConfigurationDraftResponseDTO(data=data, request_id=request_id, target=target)


@router.put("/candidate-configurations", response_model=McpCandidateConfigurationResponseDTO, summary="重新消费原 MCP 候选并经原来源候选／CAS 保存配置")
async def save_mcp_candidate_configuration(
    payload: McpCandidateConfigurationRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
    installation_target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    config_service: Annotated[ConfigService, Depends(get_config_service)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    target = _target(service)
    with mcp_candidate_errors():
        data = await owner.save_candidate_configuration(payload, actual_target=target,
            installation_target=installation_target, config_service=config_service)
    return McpCandidateConfigurationResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/candidate-configurations/plans", response_model=McpCandidatePlanResponseDTO,
    summary="在明确工作区重新规划 MCP 配置，消费该工作区私有输入")
async def plan_mcp_candidate_configuration(
    payload: McpCandidatePlanRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with mcp_candidate_errors():
        data = await owner.prepare_candidate_configuration(payload, actual_target=target)
    return McpCandidatePlanResponseDTO(data=data, request_id=request_id, target=target)


@router.get(
    "/servers",
    response_model=McpServerListResponseDTO,
    summary="获取 MCP Server 与工具目录",
)
async def list_mcp_servers(
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
    refresh: Annotated[bool, Query(description="默认主动刷新无变更通知的 MCP 服务；false 仅读取已发布目录快照")] = True,
):
    if refresh:
        try:
            await owner.relist_servers_without_notifications()
        except McpCatalogRelistError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
    snapshot = owner.management_snapshot()
    if not refresh:
        errors = [server.last_relist_error for server in snapshot.servers if server.last_relist_error]
        if errors:
            raise HTTPException(status_code=503, detail="; ".join(errors))
    servers = [
        McpServerDTO(
            server_id=server.server_id,
            transport=server.transport,
            enabled=server.enabled,
            status=server.status,
            connection_ref=server.connection_ref,
            tools=[
                McpToolDTO(
                    tool_id=tool.tool_id,
                    server_id=tool.server_id,
                    remote_name=tool.remote_name,
                    description=tool.description,
                )
                for tool in server.tools
            ],
        )
        for server in snapshot.servers
    ]
    return McpServerListResponseDTO(
        data=servers, request_id=request_id, target=_target(service),
        catalog_revision=snapshot.catalog_revision, catalog_generation=snapshot.catalog_generation,
    )


@contextmanager
def _connection_errors():
    try:
        yield
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValidationError:
        raise
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except TimeoutError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@router.get("/connection-operations", response_model=McpConnectionOperationListResponseDTO)
async def list_connection_operations(
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    return McpConnectionOperationListResponseDTO(data=service.list_operations(), request_id=request_id, target=_target(service))


@router.post("/connection-operations", response_model=McpConnectionOperationResponseDTO)
async def create_connection_operation(
    body: McpConnectionRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    with _connection_errors():
        return McpConnectionOperationResponseDTO(data=await service.submit(body), request_id=request_id, target=_target(service))


@router.get("/connection-operations/{operation_id}", response_model=McpConnectionOperationResponseDTO)
async def get_connection_operation(
    operation_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
    wait_timeout_ms: Annotated[int, Query(ge=0, le=5000)] = 0,
):
    with _connection_errors():
        return McpConnectionOperationResponseDTO(
            data=await service.status(operation_id, wait_timeout_ms=wait_timeout_ms), request_id=request_id, target=_target(service),
        )


@router.post("/connection-operations/{operation_id}/cancel", response_model=McpConnectionOperationResponseDTO)
async def cancel_connection_operation(
    operation_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    with _connection_errors():
        return McpConnectionOperationResponseDTO(data=await service.cancel(operation_id), request_id=request_id, target=_target(service))


@router.get("/connection-operations/{operation_id}/authorization", response_model=McpAuthorizationResponseDTO)
async def get_connection_authorization(
    operation_id: str,
    response: Response,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    response.headers["Cache-Control"] = "no-store"
    with _connection_errors():
        return McpAuthorizationResponseDTO(data=service.authorization(operation_id), request_id=request_id, target=_target(service))


@router.post("/connection-operations/{operation_id}/callback", response_model=McpConnectionOperationResponseDTO)
async def complete_connection_authorization(
    operation_id: str,
    body: McpAuthorizationCallbackDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[McpConnectionService, Depends(get_mcp_connection_service)],
):
    with _connection_errors():
        return McpConnectionOperationResponseDTO(
            data=service.complete_authorization(operation_id, code=body.code, state=body.state), request_id=request_id, target=_target(service),
        )
