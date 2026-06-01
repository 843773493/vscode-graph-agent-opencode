from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_config_service, get_job_service, get_message_service, get_request_id, get_session_service, verify_local_token, get_job_event_bus
from app.schemas.common import APIResponse, CursorPage
from app.schemas.message import MessageDTO, MessageRunAccepted, MessageRunRequest
from app.services.message_service import MessageService
from app.services.config_service import ConfigService
from app.services.job_service import JobService
from app.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["messages"])


@router.post("/{session_id}/messages", response_model=APIResponse[MessageRunAccepted], summary="发送消息并创建任务")
async def create_message_and_run(
    session_id: str,
    payload: MessageRunRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
    session_service: SessionService = Depends(get_session_service),
    config_service: ConfigService = Depends(get_config_service),
    job_service: JobService = Depends(get_job_service),
    job_event_bus=Depends(get_job_event_bus),
):
    result = await message_service.create_and_run(
        session_id,
        payload,
        session_service=session_service,
        config_service=config_service,
        job_service=job_service,
        job_event_bus=job_event_bus,
    )
    return APIResponse(message="ok", data=result, request_id=request_id)


@router.get("/{session_id}/messages", response_model=APIResponse[CursorPage[MessageDTO]], summary="获取消息列表")
async def list_messages(
    session_id: str,
    limit: int = 50,
    cursor: str | None = None,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
):
    result = await message_service.list(session_id=session_id, limit=limit, cursor=cursor)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{session_id}/messages/{message_id}", response_model=APIResponse[MessageDTO], summary="获取单条消息")
async def get_message(
    session_id: str,
    message_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
):
    result = await message_service.get(session_id=session_id, message_id=message_id)
    return APIResponse(data=result, request_id=request_id)
