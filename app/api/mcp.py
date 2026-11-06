from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import ValidationError
from starlette.responses import JSONResponse

from app.api.deps import (
    get_config_service,
    get_mcp_catalog_owner,
    get_mcp_connection_service,
    get_mcp_installation_target,
    get_request_id,
    verify_local_token,
)
from app.core.trace_middleware import get_current_gateway_id
from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.mcp import (
    McpArtifactReferencesResponseDTO,
    McpAuthorizationCallbackDTO,
    McpAuthorizationResponseDTO,
    McpCandidateConfigurationRequestDTO,
    McpCandidateConfigurationResponseDTO,
    McpCandidateInspectRequestDTO,
    McpCandidateInspectResponseDTO,
    McpCandidatePlanRequestDTO,
    McpCandidatePlanResponseDTO,
    McpCandidatePlanValidationRequestDTO,
    McpConnectionOperationListResponseDTO,
    McpConnectionOperationResponseDTO,
    McpConnectionRequestDTO,
    McpInstallationCapabilitiesResponseDTO,
    McpInstallationOperationResponseDTO,
    McpInstallationRequestDTO,
    McpInstallationTargetDTO,
    McpInstalledArtifactListResponseDTO,
    McpPrivateEnvWriteRequestDTO,
    McpPrivateEnvWriteResponseDTO,
    McpPrivateHeaderWriteRequestDTO,
    McpPrivateHeaderWriteResponseDTO,
    McpServerDTO,
    McpServerListResponseDTO,
    McpToolDTO,
    McpUninstallationRequestDTO,
    McpWorkspaceTargetDTO,
)
from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.mcp import McpCatalogOwner, McpCatalogRelistError
from app.services.infrastructure.mcp.candidate_source import (
    McpCandidateSourceUnavailable,
)
from app.services.infrastructure.mcp.connection_service import McpConnectionService
from app.services.infrastructure.mcp.installation.python import InstallationUnavailable
from app.services.infrastructure.mcp.installation.references import McpArtifactInUse

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _target(service: McpConnectionService) -> McpWorkspaceTargetDTO:
    return McpWorkspaceTargetDTO(gateway_id=get_current_gateway_id(), workspace_id=service.workspace_id)


@contextmanager
def _candidate_errors():
    try:
        yield
    except (McpCandidateConflict, ConfigConflictError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (McpCandidateSourceUnavailable, InstallationUnavailable) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        if isinstance(error, ValidationError):
            raise
        raise HTTPException(status_code=422, detail=str(error)) from error


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
    with _candidate_errors():
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
    with _candidate_errors():
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
    with _candidate_errors():
        data = await owner.write_private_env(payload, actual_target=target, installation_target=installation_target, candidate_required=True)
    return McpPrivateEnvWriteResponseDTO(data=data, request_id=request_id, target=target)


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
    with _candidate_errors():
        data = await owner.save_candidate_configuration(payload, actual_target=target,
            installation_target=installation_target, config_service=config_service)
    return McpCandidateConfigurationResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/candidate-manifests", response_model=McpCandidateInspectResponseDTO, summary="读取 MCP 安装候选原始来源与全部 variant")
async def inspect_mcp_candidate(
    payload: McpCandidateInspectRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with _candidate_errors():
        data = await owner.inspect_installation_candidate(payload, actual_target=target)
    return McpCandidateInspectResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/candidate-plans", response_model=McpCandidatePlanResponseDTO, summary="生成绑定确切目标、manifest 与 variant 的 MCP 计划")
async def prepare_mcp_candidate(
    payload: McpCandidatePlanRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with _candidate_errors():
        data = await owner.prepare_installation_candidate(payload, actual_target=target)
    return McpCandidatePlanResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/installations", response_model=McpInstallationOperationResponseDTO, summary="在精确 Backend 准备并发布锁定 MCP 制品，不写工作区配置")
async def install_mcp_artifact(
    payload: McpInstallationRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with _candidate_errors():
        data = await owner.install_artifact(payload, actual_target=target)
    return McpInstallationOperationResponseDTO(data=data, request_id=request_id, target=target)


@router.get("/installation-capabilities", response_model=McpInstallationCapabilitiesResponseDTO, summary="读取精确 Backend 的实际 MCP 制品安装能力")
async def get_mcp_installation_capabilities(
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with _candidate_errors():
        data = owner.installations.capabilities()
    return McpInstallationCapabilitiesResponseDTO(data=data, request_id=request_id, target=target)


@router.get("/installation-operations/{operation_id}", response_model=McpInstallationOperationResponseDTO, summary="查询原 MCP 安装阶段，不重放安装")
async def get_mcp_installation_operation(
    operation_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with _candidate_errors():
        try:
            data = await asyncio.to_thread(owner.installations.operation, operation_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
    return McpInstallationOperationResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/installation-operations/{operation_id}/cancel", response_model=McpInstallationOperationResponseDTO,
    summary="独立取消原安装操作，最后消费者等待执行退出结算")
async def cancel_mcp_installation_operation(
    operation_id: str,
    payload: McpInstallationTargetDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with _candidate_errors():
        if payload != target:
            raise McpCandidateConflict("MCP 安装取消必须绑定原生 Gateway／Backend，不能改投当前目标")
        try:
            data = await owner.installations.cancel(operation_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
    return McpInstallationOperationResponseDTO(data=data, request_id=request_id, target=target)


@router.get("/installations", response_model=McpInstalledArtifactListResponseDTO, summary="读取唯一 Backend 已发布 MCP 制品记录")
async def list_mcp_installations(
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with _candidate_errors():
        data = await asyncio.to_thread(owner.installations.artifacts)
    return McpInstalledArtifactListResponseDTO(data=data, request_id=request_id, target=target)


@router.get("/installations/{installation_id}/references", response_model=McpArtifactReferencesResponseDTO,
    summary="从原工作区配置和资源账本列出精确 MCP 制品的实际保留引用")
async def get_mcp_artifact_references(
    installation_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with _candidate_errors():
        data = await asyncio.to_thread(owner.installations.references, installation_id)
    return McpArtifactReferencesResponseDTO(data=data, request_id=request_id, target=target)


@router.delete("/installations/{installation_id}", response_model=McpInstallationOperationResponseDTO,
    responses={409: {"model": McpArtifactReferencesResponseDTO}}, summary="仅在原引用全部结清后物理卸载精确 MCP generation")
async def uninstall_mcp_artifact(
    installation_id: str,
    payload: McpUninstallationRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with _candidate_errors():
        if installation_id != payload.binding.installation_id:
            raise McpCandidateConflict("MCP 卸载路径与原制品身份不一致")
        try:
            data = await owner.installations.uninstall(payload, actual_target=target)
        except McpArtifactInUse as error:
            retained = McpArtifactReferencesResponseDTO(code=409, message=str(error), data=error.references, request_id=request_id, target=target)
            return JSONResponse(status_code=409, content=retained.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
    return McpInstallationOperationResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/candidate-plans/validate", response_model=McpCandidatePlanResponseDTO, summary="消费前重新核验原 MCP 候选，不执行安装或配置")
async def validate_mcp_candidate(
    payload: McpCandidatePlanValidationRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpCatalogOwner, Depends(get_mcp_catalog_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with _candidate_errors():
        data = await owner.validate_installation_candidate(payload, actual_target=target)
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
