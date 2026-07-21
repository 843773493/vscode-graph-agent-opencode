from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import get_log_service, get_request_id, get_runtime_service, verify_local_token
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.runtime import (
    RuntimeDrainResultDTO,
    RuntimeStatusDTO,
    UiSnapshotResultDTO,
)
from app.services.infrastructure.log_service import LogService, LogSnapshotRecord
from app.services.infrastructure.runtime_service import RuntimeService

router = APIRouter(prefix="/runtime", tags=["runtime"])


class UiSnapshotRequest(BaseModel):
    workspace_root: str = Field(min_length=1)
    session_id: str | None = None
    html: str = Field(min_length=1)
    page_title: str | None = None
    status: str | None = None
    source: str = "webview"


@router.get("/status", response_model=APIResponse[RuntimeStatusDTO], summary="获取运行时状态")
async def get_runtime_status(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    runtime_service: RuntimeService = Depends(get_runtime_service),
):
    result = await runtime_service.status()
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/drain",
    response_model=APIResponse[RuntimeDrainResultDTO],
    summary="关闭新 Job 准入并开始排空运行时",
)
async def begin_runtime_drain(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    runtime_service: RuntimeService = Depends(get_runtime_service),
):
    result = await runtime_service.begin_drain()
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/drain/cancel",
    response_model=APIResponse[RuntimeDrainResultDTO],
    summary="取消运行时排空并恢复新 Job 准入",
)
async def cancel_runtime_drain(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    runtime_service: RuntimeService = Depends(get_runtime_service),
):
    result = await runtime_service.cancel_drain()
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/drain/force",
    response_model=APIResponse[RuntimeDrainResultDTO],
    summary="显式中断阻塞资源并准备强制重启",
)
async def force_runtime_drain(
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    runtime_service: RuntimeService = Depends(get_runtime_service),
):
    result = await runtime_service.force_interrupt()
    return APIResponse(data=result, request_id=request_id)


@router.post("/log-snapshot", response_model=APIResponse[UiSnapshotResultDTO], summary="保存前端 HTML 快照")
async def save_log_snapshot(
    payload: UiSnapshotRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    runtime_service: RuntimeService = Depends(get_runtime_service),
    log_service: LogService = Depends(get_log_service),
):
    _ = runtime_service.get_log_dir()
    result = log_service.write_html_snapshot(
        LogSnapshotRecord(
            workspace_root=payload.workspace_root,
            session_id=payload.session_id,
            html=payload.html,
            page_title=payload.page_title,
            status=payload.status,
            source=payload.source,
            category="webview",
        )
    )
    import logging

    logging.info("UI HTML 快照已落盘: %s", result.get("html_path"))
    return APIResponse(data=result, request_id=request_id)
