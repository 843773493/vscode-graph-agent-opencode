from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, get_tool_service, verify_local_token
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.tool import ToolDTO, ToolInvokeRequest, ToolInvokeResultDTO
from app.services.infrastructure.tool_service import ToolService

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=APIResponse[list[ToolDTO]], summary="获取 Tool 列表")
async def list_tools(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    tool_service: ToolService = Depends(get_tool_service),
):
    result = await tool_service.list()
    return APIResponse(data=result, request_id=request_id)


@router.get("/{tool_id}", response_model=APIResponse[ToolDTO], summary="获取 Tool 详情")
async def get_tool(
    tool_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    tool_service: ToolService = Depends(get_tool_service),
):
    result = await tool_service.get(tool_id)
    return APIResponse(data=result, request_id=request_id)


@router.post("/{tool_id}/invoke", response_model=APIResponse[ToolInvokeResultDTO], summary="调用 Tool")
async def invoke_tool(
    tool_id: str,
    payload: ToolInvokeRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    tool_service: ToolService = Depends(get_tool_service),
):
    result = await tool_service.invoke(tool_id, payload)
    return APIResponse(data=result, request_id=request_id)
