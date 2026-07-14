from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_request_id, get_workspace_service, verify_local_token
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.workspace import (
    WorkspaceContextDTO,
    WorkspaceDTO,
    WorkspaceFileContentDTO,
    WorkspaceFileListDTO,
    WorkspaceIndexRebuildDTO,
    WorkspaceIndexStatusDTO,
)
from app.services.infrastructure.workspace_service import WorkspaceService

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.get("", response_model=APIResponse[WorkspaceDTO], summary="获取当前工作区信息")
async def get_workspace(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    result = await workspace_service.get()
    return APIResponse(data=result, request_id=request_id)


@router.get("/context", response_model=APIResponse[WorkspaceContextDTO], summary="获取工作区上下文")
async def get_workspace_context(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    result = await workspace_service.get_context()
    return APIResponse(data=result, request_id=request_id)


@router.get("/index", response_model=APIResponse[WorkspaceIndexStatusDTO], summary="获取工作区索引状态")
async def get_workspace_index(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    result = await workspace_service.get_index_status()
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/index/rebuild",
    response_model=APIResponse[WorkspaceIndexRebuildDTO],
    summary="重建工作区索引",
)
async def rebuild_workspace_index(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    result = await workspace_service.rebuild_index()
    return APIResponse(data=result, request_id=request_id)


@router.get("/files", response_model=APIResponse[WorkspaceFileListDTO], summary="获取工作区文件树目录")
async def list_workspace_files(
    path: str = Query(default="", description="相对工作区根目录的目录路径"),
    limit: int = Query(
        default=500,
        ge=1,
        le=1000,
        description="单个目录最多返回的子项数量",
    ),
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    result = await workspace_service.list_files(path=path, limit=limit)
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/files/content",
    response_model=APIResponse[WorkspaceFileContentDTO],
    summary="获取工作区文件预览内容",
)
async def get_workspace_file_content(
    path: str = Query(description="相对工作区根目录的文件路径"),
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        result = await workspace_service.get_file_content(path=path)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (IsADirectoryError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)
