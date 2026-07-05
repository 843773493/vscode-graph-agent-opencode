from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import get_context_compaction_service, get_request_id, get_session_auto_continue_service, get_session_interrupt_service, get_session_service, verify_local_token
from app.schemas.public_v2.common import APIResponse, CursorPage
from app.schemas.public_v2.session import (
    DeleteSessionResultDTO,
    SessionAutoContinueStartRequest,
    SessionAutoContinueStatusDTO,
    SessionCreateRequest,
    SessionDTO,
    SessionCompactResultDTO,
    SessionInterruptResultDTO,
    SessionUpdateRequest,
)
from app.schemas.public_v2.trace import TraceEventDTO
from app.services.business.session_interrupt_service import SessionInterruptService
from app.services.business.context_compaction_service import ContextCompactionService
from app.services.orchestration.session_auto_continue_service import SessionAutoContinueService
from app.services.business.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=APIResponse[SessionDTO], summary="创建会话")
async def create_session(
    payload: SessionCreateRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
):
    result = await session_service.create(payload)
    return APIResponse(data=result, request_id=request_id)


@router.get("", response_model=APIResponse[CursorPage[SessionDTO]], summary="获取会话列表")
async def list_sessions(
    limit: int = 20,
    cursor: str | None = None,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
):
    result = await session_service.list(limit=limit, cursor=cursor)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{session_id}", response_model=APIResponse[SessionDTO], summary="获取会话详情")
async def get_session(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
):
    result = await session_service.get(session_id)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{session_id}/traces", response_model=APIResponse[list[TraceEventDTO]], summary="获取会话执行轨迹")
async def list_session_traces(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
):
    result = await session_service.list_trace_events(session_id)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{session_id}/traces/stream", summary="订阅会话执行轨迹流")
async def stream_session_traces(
    session_id: str,
    _: str = Depends(verify_local_token),
    session_service: SessionService = Depends(get_session_service),
):
    async def event_generator():
        async for event in session_service.stream_trace_events(session_id):
            if hasattr(event, "model_dump_json"):
                data = event.model_dump_json()
            else:
                import json

                data = json.dumps(event, ensure_ascii=False, default=str)

            yield f"event: trace\n"
            yield f"data: {data}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.patch("/{session_id}", response_model=APIResponse[SessionDTO], summary="更新会话")
async def update_session(
    session_id: str,
    payload: SessionUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
):
    result = await session_service.update(session_id, payload)
    return APIResponse(data=result, request_id=request_id)


@router.delete("/{session_id}", response_model=APIResponse[DeleteSessionResultDTO], summary="删除会话")
async def delete_session(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    session_service: SessionService = Depends(get_session_service),
):
    result = await session_service.delete(session_id)
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/auto-continue/start",
    response_model=APIResponse[SessionAutoContinueStatusDTO],
    summary="开启会话自动继续任务",
)
async def start_session_auto_continue(
    session_id: str,
    payload: SessionAutoContinueStartRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    auto_continue_service: SessionAutoContinueService = Depends(get_session_auto_continue_service),
):
    result = await auto_continue_service.start(session_id=session_id, poll_interval_seconds=payload.poll_interval_seconds)
    return APIResponse(message="accepted", data=result, request_id=request_id)


@router.post(
    "/{session_id}/auto-continue/stop",
    response_model=APIResponse[SessionAutoContinueStatusDTO],
    summary="关闭会话自动继续任务",
)
async def stop_session_auto_continue(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    auto_continue_service: SessionAutoContinueService = Depends(get_session_auto_continue_service),
):
    result = await auto_continue_service.stop(session_id=session_id)
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/auto-continue",
    response_model=APIResponse[SessionAutoContinueStatusDTO],
    summary="获取会话自动继续任务状态",
)
async def get_session_auto_continue_status(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    auto_continue_service: SessionAutoContinueService = Depends(get_session_auto_continue_service),
):
    result = await auto_continue_service.get_status(session_id=session_id)
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/compact",
    response_model=APIResponse[SessionCompactResultDTO],
    summary="压缩会话上下文",
)
async def compact_session_context(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    context_compaction_service: ContextCompactionService = Depends(get_context_compaction_service),
):
    result = await context_compaction_service.compact(session_id=session_id)
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/interrupt",
    response_model=APIResponse[SessionInterruptResultDTO],
    summary="打断会话正在执行的任务",
)
async def interrupt_session(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    session_interrupt_service: SessionInterruptService = Depends(get_session_interrupt_service),
):
    try:
        result = await session_interrupt_service.interrupt(session_id=session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return APIResponse(data=result, request_id=request_id)
