from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.common import APIResponse, CursorPage
from app.schemas.message import MessageDTO, MessageRunAccepted, MessageRunRequest
from app.services.message_service import MessageService

router = APIRouter(prefix="/sessions", tags=["messages"])


@router.post("/{session_id}/messages", response_model=APIResponse[MessageRunAccepted], summary="发送消息并创建任务")
async def create_message_and_run(
    session_id: str,
    payload: MessageRunRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await MessageService.get_instance().create_and_run(session_id, payload)
    return APIResponse(message="accepted", data=result, request_id=request_id)


@router.get("/{session_id}/messages", response_model=APIResponse[CursorPage[MessageDTO]], summary="获取消息列表")
async def list_messages(
    session_id: str,
    limit: int = 50,
    cursor: str | None = None,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await MessageService.get_instance().list(session_id=session_id, limit=limit, cursor=cursor)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{session_id}/messages/{message_id}", response_model=APIResponse[MessageDTO], summary="获取单条消息")
async def get_message(
    session_id: str,
    message_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await MessageService.get_instance().get(session_id=session_id, message_id=message_id)
    return APIResponse(data=result, request_id=request_id)
