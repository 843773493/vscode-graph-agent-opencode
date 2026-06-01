from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import get_request_id, verify_local_token
from app.schemas.common import APIResponse
from app.services.log_service import LogService, LogSnapshotRecord
from app.services.runtime_service import RuntimeService

router = APIRouter(prefix="/runtime", tags=["runtime"])


class UiSnapshotRequest(BaseModel):
    workspace_root: str = Field(min_length=1)
    session_id: str | None = None
    html: str = Field(min_length=1)
    page_title: str | None = None
    status: str | None = None
    source: str = "webview"


@router.get("/status", response_model=APIResponse[dict], summary="获取运行时状态")
async def get_runtime_status(
    request: Request,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    runtime_service: RuntimeService = request.app.state.container.runtime_service
    result = await runtime_service.status()
    return APIResponse(data=result, request_id=request_id)


@router.post("/shutdown", response_model=APIResponse[dict], summary="关闭运行时")
async def shutdown_runtime(
    request: Request,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    runtime_service: RuntimeService = request.app.state.container.runtime_service
    result = await runtime_service.shutdown()
    return APIResponse(data=result, request_id=request_id)


@router.post("/log-snapshot", response_model=APIResponse[dict], summary="保存前端 HTML 快照")
async def save_log_snapshot(
    request: Request,
    payload: UiSnapshotRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    runtime_service: RuntimeService = request.app.state.container.runtime_service
    log_service: LogService = request.app.state.container.log_service
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
