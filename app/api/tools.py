from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.common import APIResponse
from app.schemas.tool import ToolDTO, ToolInvokeRequest
from app.services.tool_service import ToolService

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=APIResponse[list[ToolDTO]], summary="获取 Tool 列表")
async def list_tools(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await ToolService().list()
    return APIResponse(data=result, request_id=request_id)


@router.get("/{tool_id}", response_model=APIResponse[ToolDTO], summary="获取 Tool 详情")
async def get_tool(
    tool_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await ToolService().get(tool_id)
    return APIResponse(data=result, request_id=request_id)


@router.post("/{tool_id}/invoke", response_model=APIResponse[dict], summary="调用 Tool")
async def invoke_tool(
    tool_id: str,
    payload: ToolInvokeRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await ToolService().invoke(tool_id, payload)
    return APIResponse(data=result, request_id=request_id)
