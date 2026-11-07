"""Backend 进程级 MCP 制品入口；不装配工作区业务图。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import ValidationError
from starlette.responses import JSONResponse

from app.api.deps import (
    get_mcp_installation_owner,
    get_mcp_installation_target,
    get_request_id,
    verify_local_token,
)
from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.mcp import (
    McpArtifactReferencesResponseDTO,
    McpCandidateInspectRequestDTO,
    McpCandidateInspectResponseDTO,
    McpCandidatePlanRequestDTO,
    McpCandidatePlanResponseDTO,
    McpCandidatePlanValidationRequestDTO,
    McpInstallationCapabilitiesResponseDTO,
    McpInstallationOperationResponseDTO,
    McpInstallationRequestDTO,
    McpInstallationTargetDTO,
    McpInstalledArtifactListResponseDTO,
    McpUninstallationRequestDTO,
)
from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.mcp.candidate_source import (
    McpCandidateSourceUnavailable,
)
from app.services.infrastructure.mcp.installation.owner import McpInstallationOwner
from app.services.infrastructure.mcp.installation.references import McpArtifactInUse
from app.services.infrastructure.mcp.installation.runtime import InstallationUnavailable

router = APIRouter(prefix="/mcp", tags=["mcp-installation"])


@contextmanager
def mcp_candidate_errors():
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


@router.post("/candidate-manifests", response_model=McpCandidateInspectResponseDTO, summary="读取 MCP 安装候选原始来源与全部 variant")
async def inspect_mcp_candidate(
    payload: McpCandidateInspectRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpInstallationOwner, Depends(get_mcp_installation_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with mcp_candidate_errors():
        data = await owner.inspect_candidate(payload, actual_target=target)
    return McpCandidateInspectResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/candidate-plans", response_model=McpCandidatePlanResponseDTO, summary="生成绑定确切目标、manifest 与 variant 的 MCP 计划")
async def prepare_mcp_candidate(
    payload: McpCandidatePlanRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpInstallationOwner, Depends(get_mcp_installation_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with mcp_candidate_errors():
        data = await owner.plan_candidate(payload, actual_target=target)
    return McpCandidatePlanResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/installations", response_model=McpInstallationOperationResponseDTO, summary="在精确 Backend 准备并发布锁定 MCP 制品，不写工作区配置")
async def install_mcp_artifact(
    payload: McpInstallationRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpInstallationOwner, Depends(get_mcp_installation_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with mcp_candidate_errors():
        data = await owner.install_candidate(payload, actual_target=target)
    return McpInstallationOperationResponseDTO(data=data, request_id=request_id, target=target)


@router.get("/installation-capabilities", response_model=McpInstallationCapabilitiesResponseDTO, summary="读取精确 Backend 的实际 MCP 制品安装能力")
async def get_mcp_installation_capabilities(
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpInstallationOwner, Depends(get_mcp_installation_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with mcp_candidate_errors():
        data = owner.capabilities()
    return McpInstallationCapabilitiesResponseDTO(data=data, request_id=request_id, target=target)


@router.get("/installation-operations/{operation_id}", response_model=McpInstallationOperationResponseDTO, summary="查询原 MCP 安装阶段，不重放安装")
async def get_mcp_installation_operation(
    operation_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpInstallationOwner, Depends(get_mcp_installation_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with mcp_candidate_errors():
        try:
            data = await asyncio.to_thread(owner.operation, operation_id)
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
    owner: Annotated[McpInstallationOwner, Depends(get_mcp_installation_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with mcp_candidate_errors():
        if payload != target:
            raise McpCandidateConflict("MCP 安装取消必须绑定原生 Gateway／Backend，不能改投当前目标")
        try:
            data = await owner.cancel(operation_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
    return McpInstallationOperationResponseDTO(data=data, request_id=request_id, target=target)


@router.get("/installations", response_model=McpInstalledArtifactListResponseDTO, summary="读取唯一 Backend 已发布 MCP 制品记录")
async def list_mcp_installations(
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpInstallationOwner, Depends(get_mcp_installation_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with mcp_candidate_errors():
        data = await asyncio.to_thread(owner.artifacts)
    return McpInstalledArtifactListResponseDTO(data=data, request_id=request_id, target=target)


@router.get("/installations/{installation_id}/references", response_model=McpArtifactReferencesResponseDTO,
    summary="从原工作区配置和资源账本列出精确 MCP 制品的实际保留引用")
async def get_mcp_artifact_references(
    installation_id: str,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpInstallationOwner, Depends(get_mcp_installation_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with mcp_candidate_errors():
        data = await asyncio.to_thread(owner.references, installation_id)
    return McpArtifactReferencesResponseDTO(data=data, request_id=request_id, target=target)


@router.delete("/installations/{installation_id}", response_model=McpInstallationOperationResponseDTO,
    responses={409: {"model": McpArtifactReferencesResponseDTO}}, summary="仅在原引用全部结清后物理卸载精确 MCP generation")
async def uninstall_mcp_artifact(
    installation_id: str,
    payload: McpUninstallationRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpInstallationOwner, Depends(get_mcp_installation_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with mcp_candidate_errors():
        if installation_id != payload.binding.installation_id:
            raise McpCandidateConflict("MCP 卸载路径与原制品身份不一致")
        try:
            data = await owner.uninstall(payload, actual_target=target)
        except McpArtifactInUse as error:
            retained = McpArtifactReferencesResponseDTO(code=409, message=str(error), data=error.references, request_id=request_id, target=target)
            return JSONResponse(status_code=409, content=retained.model_dump(mode="json"), headers={"Cache-Control": "no-store"})
    return McpInstallationOperationResponseDTO(data=data, request_id=request_id, target=target)


@router.post("/candidate-plans/validate", response_model=McpCandidatePlanResponseDTO, summary="消费前重新核验原 MCP 候选，不执行安装或配置")
async def validate_mcp_candidate(
    payload: McpCandidatePlanValidationRequestDTO,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    owner: Annotated[McpInstallationOwner, Depends(get_mcp_installation_owner)],
    target: Annotated[McpInstallationTargetDTO, Depends(get_mcp_installation_target)],
    response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    with mcp_candidate_errors():
        data = await owner.validate_candidate(payload, actual_target=target)
    return McpCandidatePlanResponseDTO(data=data, request_id=request_id, target=target)
