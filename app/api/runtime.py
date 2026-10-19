from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.abstractions.state_conflict import ClientStateConflictError
from app.api.deps import (
    get_log_service,
    get_request_id,
    get_runtime_service,
    verify_local_token,
)
from app.api.errors import state_conflict_error
from app.schemas.canonical_params import CanonicalSessionId
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.runtime import (
    RuntimeDrainResultDTO,
    RuntimeStatusDTO,
    UiSnapshotResultDTO,
)
from app.services.infrastructure.log_service import LogService, LogSnapshotRecord
from app.services.infrastructure.runtime_service import RuntimeService

router = APIRouter(prefix="/runtime", tags=["runtime"])
logger = logging.getLogger(__name__)


class UiSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: CanonicalSessionId | None = None
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
    try:
        result = await runtime_service.begin_drain()
    except ClientStateConflictError as error:
        # 生命周期状态冲突（如 stopping 下再次 drain）是客户端时序错误，落 409。
        raise state_conflict_error(error) from error
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
    try:
        result = await runtime_service.cancel_drain()
    except ClientStateConflictError as error:
        # 非 draining 状态下取消排空属于状态冲突，落 409。
        raise state_conflict_error(error) from error
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
    try:
        result = await runtime_service.force_interrupt()
    except ClientStateConflictError as error:
        # 非 draining 状态下强制中断属于状态冲突，落 409。
        raise state_conflict_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/log-snapshot", response_model=APIResponse[UiSnapshotResultDTO], summary="保存前端 HTML 快照")
async def save_log_snapshot(
    payload: UiSnapshotRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    log_service: LogService = Depends(get_log_service),
):
    try:
        result = await log_service.write_html_snapshot(
            LogSnapshotRecord(
                session_id=payload.session_id,
                html=payload.html,
                page_title=payload.page_title,
                status=payload.status,
                source=payload.source,
            )
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    logger.info("UI HTML 快照已保存: %s", result.snapshot_id)
    return APIResponse(data=result, request_id=request_id)


@router.get("/log-snapshot/{snapshot_id}/html", response_class=HTMLResponse, summary="读取前端 HTML 快照")
async def read_log_snapshot_html(
    snapshot_id: str,
    session_id: CanonicalSessionId | None = None,
    _: str = Depends(verify_local_token),
    log_service: LogService = Depends(get_log_service),
):
    content = await _read_log_snapshot(log_service, snapshot_id, session_id, metadata=False)
    return Response(content, media_type="text/html", headers={
        "Content-Disposition": f'attachment; filename="{snapshot_id}.html"',
        "Content-Security-Policy": "sandbox; default-src 'none'",
    })


@router.get("/log-snapshot/{snapshot_id}/metadata", summary="读取前端快照 metadata")
async def read_log_snapshot_metadata(
    snapshot_id: str,
    session_id: CanonicalSessionId | None = None,
    _: str = Depends(verify_local_token),
    log_service: LogService = Depends(get_log_service),
):
    content = await _read_log_snapshot(log_service, snapshot_id, session_id, metadata=True)
    return Response(content, media_type="application/json")


async def _read_log_snapshot(
    log_service: LogService, snapshot_id: str, session_id: str | None, *, metadata: bool,
) -> str:
    try:
        return await log_service.read_html_snapshot(snapshot_id, session_id=session_id, metadata=metadata)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
