from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from app.abstractions.job_service import JobServiceProtocol
from app.api.canonical_params import CanonicalSessionId, CanonicalThreadId
from app.api.deps import (
    get_attachment_blob_store,
    get_job_service,
    get_message_service,
    get_request_id,
    get_session_communication_service,
    get_session_orchestrator,
    get_session_turn_replay_service,
    verify_local_token,
)
from app.api.errors import not_found_http_error, state_conflict_http_error
from app.core.exceptions import NotFoundError
from app.runtime.session_orchestrator import SessionOrchestrator
from app.schemas.internal_v2.common import APIResponse, CursorPage
from app.schemas.internal_v2.communication import (
    CommunicationAcceptanceRequest,
    CommunicationObservationRequest,
    CommunicationReceipt,
)
from app.schemas.internal_v2.message import (
    AgentStateMessagesDTO,
    MessageDTO,
    MessageReplayAccepted,
    MessageReplayRequest,
    MessageRunAccepted,
    MessageRunRequest,
)
from app.schemas.internal_v2.pending_request import (
    PendingRequestListDTO,
    PendingRequestPolicyUpdateRequest,
    PendingRequestUpdateRequest,
)
from app.services.business.job.service import JobAdmissionClosedError
from app.services.business.message_service import MessageService
from app.services.business.session_turn_replay_service import SessionTurnReplayService
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.orchestration.communication.service import SessionCommunicationService

router = APIRouter(prefix="/sessions", tags=["messages"])
logger = logging.getLogger(__name__)


@router.get(
    "/{session_id}/pending-requests",
    response_model=APIResponse[PendingRequestListDTO],
    summary="获取会话待处理消息",
)
async def list_pending_requests(
    session_id: CanonicalSessionId,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    try:
        result = await job_service.list_pending(session_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.patch(
    "/{session_id}/pending-requests/{message_id}",
    response_model=APIResponse[PendingRequestListDTO],
    summary="编辑待处理消息",
)
async def update_pending_request(
    session_id: CanonicalSessionId,
    message_id: str,
    payload: PendingRequestUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
    attachment_store: AttachmentBlobStore = Depends(get_attachment_blob_store),
):
    async def update_prepared() -> PendingRequestListDTO:
        attachments = await attachment_store.persist_inline(
            session_id, payload.attachments
        )
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
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.delete(
    "/{session_id}/pending-requests/{message_id}",
    response_model=APIResponse[PendingRequestListDTO],
    summary="从队列撤回消息",
)
async def remove_pending_request(
    session_id: CanonicalSessionId,
    message_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    try:
        result = await job_service.remove_pending(session_id, message_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RuntimeError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.delete(
    "/{session_id}/pending-requests",
    response_model=APIResponse[PendingRequestListDTO],
    summary="清空会话待处理消息",
)
async def clear_pending_requests(
    session_id: CanonicalSessionId,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    try:
        result = await job_service.clear_pending(session_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except RuntimeError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.patch(
    "/{session_id}/pending-requests/{message_id}/policy",
    response_model=APIResponse[PendingRequestListDTO],
    summary="修改待处理消息投递策略",
)
async def update_pending_request_policy(
    session_id: CanonicalSessionId,
    message_id: str,
    payload: PendingRequestPolicyUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    job_service: JobServiceProtocol = Depends(get_job_service),
):
    try:
        result = await job_service.update_pending_policy(
            session_id,
            message_id,
            delivery_policy=payload.delivery_policy,
            expected_snapshot_version=payload.expected_snapshot_version,
        )
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except ValueError as error:
        raise state_conflict_http_error(error) from error
    except RuntimeError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/messages",
    response_model=APIResponse[MessageRunAccepted],
    summary="发送消息并创建任务",
)
async def create_message_and_run(
    session_id: CanonicalSessionId,
    payload: MessageRunRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
    session_orchestrator: SessionOrchestrator = Depends(get_session_orchestrator),
):
    try:
        result = await session_orchestrator.create_message(session_id, payload)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except JobAdmissionClosedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return APIResponse(message="ok", data=result, request_id=request_id)


@router.post(
    "/{session_id}/threads/{thread_id}/messages",
    response_model=APIResponse[MessageRunAccepted],
    summary="向精确 thread 发送消息并创建任务",
)
async def create_thread_message_and_run(
    session_id: CanonicalSessionId, thread_id: CanonicalThreadId, payload: MessageRunRequest,
    _: Annotated[str, Depends(verify_local_token)], request_id: Annotated[str, Depends(get_request_id)],
    session_orchestrator: Annotated[SessionOrchestrator, Depends(get_session_orchestrator)],
):
    try:
        result = await session_orchestrator.create_message(session_id, payload, thread_id=thread_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except JobAdmissionClosedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/inter-agent-messages", response_model=APIResponse[CommunicationReceipt],
    summary="持久接收来自受认证 Gateway origin 的通信",
)
async def dispatch_inter_agent_message(
    session_id: CanonicalSessionId, payload: CommunicationAcceptanceRequest,
    origin: Annotated[str, Header(alias="X-BoxTeam-Communication-Origin")],
    _: Annotated[str, Depends(verify_local_token)], request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[SessionCommunicationService, Depends(get_session_communication_service)],
):
    if payload.target.session_id != session_id or payload.source.gateway_id != origin:
        raise HTTPException(status_code=400, detail="通信 path/source 与认证 origin 不一致")
    try:
        result = await service.accept(payload)
    except (ValueError, RuntimeError, KeyError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/{session_id}/communication-observations")
async def observe_communication(
    session_id: CanonicalSessionId, payload: CommunicationObservationRequest,
    origin: Annotated[str, Header(alias="X-BoxTeam-Communication-Origin")],
    _: Annotated[str, Depends(verify_local_token)], request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[SessionCommunicationService, Depends(get_session_communication_service)],
):
    if payload.target.session_id != session_id or payload.source.gateway_id != origin:
        raise HTTPException(status_code=400, detail="观察 path/source 与认证 origin 不一致")
    from app.core.trace_middleware import require_current_gateway_id
    if payload.target.gateway_id != require_current_gateway_id("target observation"):
        raise HTTPException(status_code=400, detail="观察 Gateway audience 不一致")
    from app.core.session_control_communication_ledger.observations import (
        RemoteObservationError,
    )
    try:
        result = await service.observations.observe(payload)
    except RemoteObservationError as error:
        raise HTTPException(status_code=409, detail=error.to_wire()) from error
    except (ValueError, RuntimeError, KeyError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/messages",
    response_model=APIResponse[CursorPage[MessageDTO]],
    summary="获取消息列表",
)
async def list_messages(
    session_id: CanonicalSessionId,
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
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except ValueError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get("/{session_id}/attachments/content", summary="读取会话附件")
async def get_session_attachment_content(
    session_id: CanonicalSessionId,
    file_id: str = Query(min_length=1),
    variant: str = Query(default="original", pattern="^(original|thumbnail)$"),
    max_edge: int = Query(default=512, ge=64, le=1024),
    _: str = Depends(verify_local_token),
    attachment_store: AttachmentBlobStore = Depends(get_attachment_blob_store),
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
        raise state_conflict_http_error(error) from error
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
    session_id: CanonicalSessionId,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
):
    try:
        result = await message_service.get_agent_state_messages(session_id=session_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/messages/{message_id}",
    response_model=APIResponse[MessageDTO],
    summary="获取单条消息",
)
async def get_message(
    session_id: CanonicalSessionId,
    message_id: str,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    message_service: MessageService = Depends(get_message_service),
):
    try:
        result = await message_service.get(
            session_id=session_id, message_id=message_id
        )
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/messages/{message_id}/replay",
    response_model=APIResponse[MessageReplayAccepted],
    summary="重试、重新生成或编辑指定用户轮次",
)
async def replay_message_turn(
    session_id: CanonicalSessionId,
    message_id: str,
    payload: MessageReplayRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    replay_service: SessionTurnReplayService = Depends(get_session_turn_replay_service),
):
    try:
        result = await replay_service.replay(session_id, message_id, payload)
    except (KeyError, NotFoundError) as exc:
        # _assert_session_idle 先经 JobService.list 解析会话物理目录，缺失时抛 KeyError。
        raise not_found_http_error(exc) from exc
    except ValueError as exc:
        logger.exception(
            "消息重放失败: session_id=%s message_id=%s action=%s",
            session_id,
            message_id,
            payload.action,
        )
        raise state_conflict_http_error(exc) from exc
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/turns/{turn_id}/replay",
    response_model=APIResponse[MessageReplayAccepted],
    summary="重试、重新生成或编辑指定 Turn",
)
async def replay_turn(
    session_id: CanonicalSessionId,
    turn_id: str,
    payload: MessageReplayRequest,
    _: str = Depends(verify_local_token),
    request_id: str = Depends(get_request_id),
    replay_service: SessionTurnReplayService = Depends(get_session_turn_replay_service),
):
    try:
        result = await replay_service.replay_turn(session_id, turn_id, payload)
    except (KeyError, NotFoundError) as exc:
        # 会话缺失时由 message/job 链路的 catalog 解析抛 KeyError。
        raise not_found_http_error(exc) from exc
    except ValueError as exc:
        logger.exception(
            "Turn 重放失败: session_id=%s turn_id=%s action=%s",
            session_id,
            turn_id,
            payload.action,
        )
        raise state_conflict_http_error(exc) from exc
    return APIResponse(data=result, request_id=request_id)
