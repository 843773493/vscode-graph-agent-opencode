from __future__ import annotations

import asyncio
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import (
    get_request_id,
    get_workspace_file_watch_service,
    get_workspace_service,
    verify_local_token,
)
from app.core.exceptions import ForbiddenError
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.sse import SseErrorDTO, sse_responses
from app.schemas.public_v2.workspace import (
    WorkspaceContextDTO,
    WorkspaceDTO,
    WorkspaceFileChangeBatchDTO,
    WorkspaceFileChangeDTO,
    WorkspaceFileContentDTO,
    WorkspaceFileCreateRequest,
    WorkspaceFileListDTO,
    WorkspaceFilePasteRequest,
    WorkspaceFileRevealDTO,
    WorkspaceFileScope,
    WorkspaceFileUpdateRequest,
    WorkspaceFileWatchRequest,
    WorkspaceIndexRebuildDTO,
    WorkspaceIndexStatusDTO,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileWatchService,
)
from app.services.infrastructure.workspace_service import (
    WorkspaceFileConflictError,
    WorkspaceService,
)

router = APIRouter(prefix="/workspace", tags=["workspace"])

WORKSPACE_FILE_STREAM_HEARTBEAT_SECONDS = 15.0


@router.post(
    "/files/events",
    response_class=StreamingResponse,
    summary="订阅工作区和快捷路径文件变更",
    responses=sse_responses(
        "SSE 文件变更事件流",
        {
            "changes": WorkspaceFileChangeBatchDTO,
            "error": SseErrorDTO,
        },
    ),
)
async def stream_workspace_file_events(
    payload: WorkspaceFileWatchRequest,
    _: str = Depends(verify_local_token),
    watch_service: WorkspaceFileWatchService = Depends(
        get_workspace_file_watch_service
    ),
):
    try:
        roots = watch_service.resolve_watch_roots(payload.paths)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (NotADirectoryError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    async def generate():
        iterator = aiter(watch_service.subscribe_roots(roots))
        next_batch = asyncio.create_task(anext(iterator))
        try:
            while True:
                completed, _ = await asyncio.wait(
                    {next_batch},
                    timeout=WORKSPACE_FILE_STREAM_HEARTBEAT_SECONDS,
                )
                if not completed:
                    yield ": heartbeat\n\n"
                    continue
                try:
                    batch = next_batch.result()
                except StopAsyncIteration:
                    return
                if batch.error is not None:
                    data = SseErrorDTO(message=batch.error).model_dump_json()
                    yield f"event: error\ndata: {data}\n\n"
                    return
                data = WorkspaceFileChangeBatchDTO(
                    overflow=batch.overflow,
                    changes=[
                        WorkspaceFileChangeDTO(
                            kind=change.kind,
                            path=change.path,
                        )
                        for change in batch.changes
                    ],
                ).model_dump_json()
                yield f"event: changes\ndata: {data}\n\n"
                next_batch = asyncio.create_task(anext(iterator))
        finally:
            if not next_batch.done():
                next_batch.cancel()
                with suppress(asyncio.CancelledError):
                    await next_batch
            await iterator.aclose()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("", response_model=APIResponse[WorkspaceDTO], summary="获取当前工作区信息")
async def get_workspace(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    result = await workspace_service.get()
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/context",
    response_model=APIResponse[WorkspaceContextDTO],
    summary="获取工作区上下文",
)
async def get_workspace_context(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    result = await workspace_service.get_context()
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/index",
    response_model=APIResponse[WorkspaceIndexStatusDTO],
    summary="获取工作区索引状态",
)
async def get_workspace_index(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
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
    request_id: str = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    result = await workspace_service.rebuild_index()
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/files",
    response_model=APIResponse[WorkspaceFileListDTO],
    summary="获取工作区文件树目录",
)
async def list_workspace_files(
    path: str = Query(default="", description="相对工作区根目录的目录路径"),
    scope: WorkspaceFileScope = Query(default="workspace"),
    limit: int = Query(
        default=500,
        ge=1,
        le=1000,
        description="单个目录最多返回的子项数量",
    ),
    cursor: str | None = Query(
        default=None,
        min_length=1,
        max_length=2048,
        description="继续读取同一目录下一页的游标",
    ),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        result = await workspace_service.list_files(
            path=path,
            scope=scope,
            limit=limit,
            cursor=cursor,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (NotADirectoryError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/files/content",
    response_model=APIResponse[WorkspaceFileContentDTO],
    summary="获取工作区文件预览内容",
)
async def get_workspace_file_content(
    path: str = Query(description="相对工作区根目录或绝对文件系统文件路径"),
    scope: WorkspaceFileScope = Query(default="workspace"),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        result = await workspace_service.get_file_content(path=path, scope=scope)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ForbiddenError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (IsADirectoryError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/files/raw",
    response_class=FileResponse,
    summary="获取工作区原始文件",
)
async def get_workspace_raw_file(
    path: str = Query(description="相对工作区根目录或绝对文件系统文件路径"),
    scope: WorkspaceFileScope = Query(default="workspace"),
    _: str = Depends(verify_local_token),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        target_path, media_type = workspace_service.resolve_raw_file(
            path=path,
            scope=scope,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ForbiddenError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (IsADirectoryError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return FileResponse(
        target_path,
        media_type=media_type,
        filename=target_path.name,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.put(
    "/files/content",
    response_model=APIResponse[WorkspaceFileContentDTO],
    summary="保存工作区文本文件",
)
async def update_workspace_file_content(
    payload: WorkspaceFileUpdateRequest,
    path: str = Query(description="相对工作区根目录或绝对文件系统文件路径"),
    scope: WorkspaceFileScope = Query(default="workspace"),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        result = await workspace_service.update_file_content(
            path=path,
            content=payload.content,
            expected_revision=payload.expected_revision,
            scope=scope,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkspaceFileConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (IsADirectoryError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/files/entries",
    response_model=APIResponse[WorkspaceFileListDTO],
    summary="在目录中创建文件或文件夹",
)
async def create_workspace_file_entry(
    payload: WorkspaceFileCreateRequest,
    path: str = Query(default="", description="目标目录路径"),
    scope: WorkspaceFileScope = Query(default="workspace"),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        result = await workspace_service.create_file_entry(
            directory_path=path,
            scope=scope,
            name=payload.name,
            kind=payload.kind,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (NotADirectoryError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/files/paste",
    response_model=APIResponse[WorkspaceFileListDTO],
    summary="把剪贴板路径对应的文件或目录复制到目标目录",
)
async def paste_workspace_file_entries(
    payload: WorkspaceFilePasteRequest,
    path: str = Query(default="", description="目标目录路径"),
    scope: WorkspaceFileScope = Query(default="workspace"),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        result = await workspace_service.paste_file_entries(
            directory_path=path,
            scope=scope,
            source_paths=payload.source_paths,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except FileExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (NotADirectoryError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except OSError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/files/reveal",
    response_model=APIResponse[WorkspaceFileRevealDTO],
    summary="在工作区主机的系统文件管理器中显示路径",
)
async def reveal_workspace_file_entry(
    path: str = Query(default="", description="需要显示的路径"),
    scope: WorkspaceFileScope = Query(default="workspace"),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    workspace_service: WorkspaceService = Depends(get_workspace_service),
):
    try:
        result = workspace_service.reveal_file_entry(path=path, scope=scope)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (ValueError, RuntimeError, OSError) as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    return APIResponse(
        data=WorkspaceFileRevealDTO(path=str(result)),
        request_id=request_id,
    )
