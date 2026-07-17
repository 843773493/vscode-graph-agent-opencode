from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    get_message_service,
    get_request_id,
    get_session_context_query_service,
    get_session_orchestrator,
    get_session_turn_replay_service,
    verify_local_token,
)
from app.schemas.public_v2.common import APIResponse, CursorPage
from app.schemas.public_v2.message import (
    AgentStateMessagesDTO,
    MessageDTO,
    MessageReplayAccepted,
    MessageReplayRequest,
    MessageRunAccepted,
    MessageRunRequest,
)
from app.schemas.public_v2.session_context import (
    SessionContextGrepRequest,
    SessionContextGrepResultDTO,
    SessionContextReadResultDTO,
    SessionRecentTextMessagesDTO,
)
from app.services.business.session_context_query_service import SessionContextQueryService
from app.services.business.message_service import MessageService
from app.services.business.session_turn_replay_service import SessionTurnReplayService
from app.runtime.session_orchestrator import SessionOrchestrator

router = APIRouter(prefix="/sessions", tags=["messages"])


@router.post("/{session_id}/messages", response_model=APIResponse[MessageRunAccepted], summary="发送消息并创建任务")
async def create_message_and_run(
    session_id: str,
    payload: MessageRunRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
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
    request_id: str = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
):
    result = await message_service.list(session_id=session_id, limit=limit, cursor=cursor)
    return APIResponse(data=result, request_id=request_id)


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


@router.get(
    "/{session_id}/context/recent-text",
    response_model=APIResponse[SessionRecentTextMessagesDTO],
    summary="读取会话当前有效上下文中的最近文本消息",
)
async def get_session_context_recent_text(
    session_id: str,
    rounds: int = Query(default=5, ge=1, le=50),
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    query_service: SessionContextQueryService = Depends(
        get_session_context_query_service
    ),
):
    result = await query_service.recent_text(session_id, rounds=rounds)
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/context/grep",
    response_model=APIResponse[SessionContextGrepResultDTO],
    summary="搜索会话当前有效上下文 JSONL",
)
async def grep_session_context(
    session_id: str,
    payload: SessionContextGrepRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    query_service: SessionContextQueryService = Depends(
        get_session_context_query_service
    ),
):
    result = await query_service.grep(
        session_id,
        pattern=payload.pattern,
        case_sensitive=payload.case_sensitive,
        max_matches=payload.max_matches,
        expected_snapshot_id=payload.expected_snapshot_id,
    )
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/context/lines",
    response_model=APIResponse[SessionContextReadResultDTO],
    summary="按行读取会话当前有效上下文 JSONL",
)
async def read_session_context_lines(
    session_id: str,
    line_start: int = Query(default=1, ge=1),
    line_count: int = Query(default=20, ge=1, le=200),
    max_chars_per_line: int = Query(default=4000, ge=200, le=20_000),
    expected_snapshot_id: str | None = None,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    query_service: SessionContextQueryService = Depends(
        get_session_context_query_service
    ),
):
    result = await query_service.read_lines(
        session_id,
        line_start=line_start,
        line_count=line_count,
        max_chars_per_line=max_chars_per_line,
        expected_snapshot_id=expected_snapshot_id,
    )
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
