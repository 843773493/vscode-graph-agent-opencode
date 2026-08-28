from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_agent_service, get_request_id, verify_local_token
from app.schemas.internal_v2.agent import (
    AgentDTO,
    WorkspaceDefaultAgentUpdateRequest,
    WorkspaceDefaultProviderUpdateRequest,
)
from app.schemas.internal_v2.common import APIResponse
from app.services.business.agent_service import AgentService

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=APIResponse[list[AgentDTO]], summary="获取 Agent 列表")
async def list_agents(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    agent_service: AgentService = Depends(get_agent_service),
):
    result = await agent_service.list()
    return APIResponse(data=result, request_id=request_id)


@router.put(
    "/workspace-default",
    response_model=APIResponse[list[AgentDTO]],
    summary="设置工作区默认 Agent",
)
async def set_workspace_default_agent(
    payload: WorkspaceDefaultAgentUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    agent_service: AgentService = Depends(get_agent_service),
):
    result = await agent_service.set_workspace_default_agent(payload.agent_id)
    return APIResponse(data=result, request_id=request_id)


@router.put(
    "/{agent_id}/workspace-default-provider",
    response_model=APIResponse[list[AgentDTO]],
    summary="设置 Agent 的工作区默认 provider",
)
async def set_workspace_default_provider(
    agent_id: str,
    payload: WorkspaceDefaultProviderUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    agent_service: AgentService = Depends(get_agent_service),
):
    result = await agent_service.set_workspace_default_provider(
        agent_id,
        payload.provider_id,
    )
    return APIResponse(data=result, request_id=request_id)


@router.get("/{agent_id}", response_model=APIResponse[AgentDTO], summary="获取 Agent 详情")
async def get_agent(
    agent_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    agent_service: AgentService = Depends(get_agent_service),
):
    result = await agent_service.get(agent_id)
    return APIResponse(data=result, request_id=request_id)
