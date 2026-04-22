from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.common import APIResponse
from app.schemas.workspace import WorkspaceContextDTO, WorkspaceDTO
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.get("", response_model=APIResponse[WorkspaceDTO], summary="获取当前工作区信息")
async def get_workspace(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await WorkspaceService.get_instance().get()
    return APIResponse(data=result, request_id=request_id)


@router.get("/context", response_model=APIResponse[WorkspaceContextDTO], summary="获取工作区上下文")
async def get_workspace_context(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await WorkspaceService.get_instance().get_context()
    return APIResponse(data=result, request_id=request_id)


@router.get("/index", response_model=APIResponse[dict], summary="获取工作区索引状态")
async def get_workspace_index(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await WorkspaceService.get_instance().get_index_status()
    return APIResponse(data=result, request_id=request_id)


@router.post("/index/rebuild", response_model=APIResponse[dict], summary="重建工作区索引")
async def rebuild_workspace_index(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await WorkspaceService.get_instance().rebuild_index()
    return APIResponse(data=result, request_id=request_id)
