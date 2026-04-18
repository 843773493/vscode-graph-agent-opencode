from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.agent import AgentDTO
from app.schemas.common import APIResponse
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=APIResponse[list[AgentDTO]], summary="获取 Agent 列表")
async def list_agents(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await AgentService().list()
    return APIResponse(data=result, request_id=request_id)


@router.get("/{agent_id}", response_model=APIResponse[AgentDTO], summary="获取 Agent 详情")
async def get_agent(
    agent_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await AgentService().get(agent_id)
    return APIResponse(data=result, request_id=request_id)
