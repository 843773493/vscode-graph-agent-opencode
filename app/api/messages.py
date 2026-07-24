from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.deps import (
    get_message_service,
    get_session_attachment_store,
    get_job_service,
    get_request_id,
    get_session_orchestrator,
    get_session_turn_replay_service,
    verify_local_token,
)
from app.abstractions.job_service import JobServiceProtocol
from app.schemas.public_v2.common import APIResponse, CursorPage
from app.schemas.public_v2.message import (
    AgentStateMessagesDTO,
    MessageDTO,
    MessageReplayAccepted,
    MessageReplayRequest,
    MessageRunAccepted,
    MessageRunRequest,
)
from app.schemas.public_v2.pending_request import (
    PendingRequestListDTO,
    PendingRequestReorderRequest,
    PendingRequestUpdateRequest,
)
from app.services.business.message_service import MessageService
from app.services.infrastructure.session_attachment_store import SessionAttachmentStore
from app.services.infrastructure.message_history_store import StaleMessageHistoryCursorError
from app.services.business.job.service import JobAdmissionClosedError
from app.services.business.session_turn_replay_service import SessionTurnReplayService
from app.runtime.session_orchestrator import SessionOrchestrator

router = APIRouter(prefix="/sessions", tags=["messages"])


@router.get(
    "/{session_id}/pending-requests",
    response_model=APIResponse[PendingRequestListDTO],
    summary="获取会话待处理消息",
)
async def list_pending_requests(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    result = await job_service.list_pending(session_id)
    return APIResponse(data=result, request_id=request_id)


@router.patch(
    "/{session_id}/pending-requests/{message_id}",
    response_model=APIResponse[PendingRequestListDTO],
    summary="编辑待处理消息",
)
async def update_pending_request(
    session_id: str,
    message_id: str,
    payload: PendingRequestUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
    attachment_store: SessionAttachmentStore = Depends(get_session_attachment_store),
):
    async def update_prepared() -> PendingRequestListDTO:
        attachments = attachment_store.persist_inline(session_id, payload.attachments)
        return await job_service.update_pending(
            session_id,
            message_id,
            content=payload.content,
            attachments=attachments,
        )

    try:
        result = await job_service.run_session_preparation(
            session_id,
            update_prepared,
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.delete(
    "/{session_id}/pending-requests/{message_id}",
    response_model=APIResponse[PendingRequestListDTO],
    summary="从队列撤回消息",
)
async def remove_pending_request(
    session_id: str,
    message_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    try:
        result = await job_service.remove_pending(session_id, message_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.delete(
    "/{session_id}/pending-requests",
    response_model=APIResponse[PendingRequestListDTO],
    summary="清空会话待处理消息",
)
async def clear_pending_requests(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    try:
        result = await job_service.clear_pending(session_id)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.put(
    "/{session_id}/pending-requests/order",
    response_model=APIResponse[PendingRequestListDTO],
    summary="重排会话待处理消息",
)
async def reorder_pending_requests(
    session_id: str,
    payload: PendingRequestReorderRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    try:
        result = await job_service.reorder_pending(session_id, payload.requests)
    except (KeyError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/pending-requests/{message_id}/send-immediately",
    response_model=APIResponse[PendingRequestListDTO],
    summary="立即发送指定待处理消息",
)
async def send_pending_request_immediately(
    session_id: str,
    message_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    try:
        result = await job_service.send_pending_immediately(
            session_id,
            message_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/{session_id}/messages", response_model=APIResponse[MessageRunAccepted], summary="发送消息并创建任务")
async def create_message_and_run(
    session_id: str,
    payload: MessageRunRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
    session_orchestrator: SessionOrchestrator = Depends(get_session_orchestrator),
):
    try:
        result = await session_orchestrator.create_message(session_id, payload)
    except JobAdmissionClosedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return APIResponse(message="ok", data=result, request_id=request_id)


@router.get("/{session_id}/messages", response_model=APIResponse[CursorPage[MessageDTO]], summary="获取消息列表")
async def list_messages(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
):
    try:
        result = await message_service.list(
            session_id=session_id,
            limit=limit,
            cursor=cursor,
        )
    except StaleMessageHistoryCursorError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get("/{session_id}/attachments/content", summary="读取会话媒体附件")
async def get_session_attachment_content(
    session_id: str,
    file_id: str = Query(min_length=1),
    variant: str = Query(default="original", pattern="^(original|thumbnail)$"),
    max_edge: int = Query(default=384, ge=64, le=1024),
    _: str = Depends(verify_local_token),
    attachment_store: SessionAttachmentStore = Depends(get_session_attachment_store),
    job_service: JobServiceProtocol = Depends(get_job_service),
) -> Response:
    async def read_content():
        return (
            attachment_store.read_thumbnail(
                session_id,
                file_id,
                max_edge=max_edge,
            )
            if variant == "thumbnail"
            else attachment_store.read(session_id, file_id)
        )

    try:
        content = await job_service.run_session_preparation(
            session_id,
            read_content,
        )
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return Response(
        content=content.data,
        media_type=content.content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get(
    "/{session_id}/agent-state/messages",
    response_model=APIResponse[AgentStateMessagesDTO],
    summary="获取 Agent State messages 快照",
)
async def get_agent_state_messages(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
):
    result = await message_service.get_agent_state_messages(session_id=session_id)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{session_id}/messages/{message_id}", response_model=APIResponse[MessageDTO], summary="获取单条消息")
async def get_message(
    session_id: str,
    message_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
):
    result = await message_service.get(session_id=session_id, message_id=message_id)
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/messages/{message_id}/replay",
    response_model=APIResponse[MessageReplayAccepted],
    summary="重试、重新生成或编辑指定用户轮次",
)
async def replay_message_turn(
    session_id: str,
    message_id: str,
    payload: MessageReplayRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    replay_service: SessionTurnReplayService = Depends(
        get_session_turn_replay_service
    ),
):
    try:
        result = await replay_service.replay(session_id, message_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return APIResponse(data=result, request_id=request_id)
