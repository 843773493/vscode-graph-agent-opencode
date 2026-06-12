from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_message_service, get_request_id, get_session_orchestrator, verify_local_token
from app.schemas.public_v2.common import APIResponse, CursorPage
from app.schemas.public_v2.message import MessageDTO, MessageRunAccepted, MessageRunRequest
from app.services.business.message_service import MessageService
from app.runtime.session_orchestrator import SessionOrchestrator

router = APIRouter(prefix="/sessions", tags=["messages"])


@router.post("/{session_id}/messages", response_model=APIResponse[MessageRunAccepted], summary="发送消息并创建任务")
async def create_message_and_run(
    session_id: str,
    payload: MessageRunRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
    session_orchestrator: SessionOrchestrator = Depends(get_session_orchestrator),
):
    result = await session_orchestrator.create_message(session_id, payload)
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
