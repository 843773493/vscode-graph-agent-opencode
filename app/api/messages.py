from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from boxteam_plugin_sdk.http import get_request_id
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.abstractions.job_service import JobServiceProtocol
from app.abstractions.session_read import SessionThreadReaderProtocol
from app.api.deps import (
    get_attachment_blob_store,
    get_job_service,
    get_message_service,
    get_rollout_checkpoint_saver,
    get_session_communication_service,
    get_session_orchestrator,
    get_session_service,
    get_session_turn_replay_service,
    verify_local_token,
)
from app.api.errors import not_found_http_error, state_conflict_http_error
from app.core.exceptions import NotFoundError
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.domain.itemized.execution_queue import (
    MessageSubmissionConflictError,
    QueueOrderConflictError,
)
from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId
from app.schemas.internal_v2.common import APIResponse, CursorPage
from app.schemas.internal_v2.communication import (
    CommunicationAcceptanceRequest,
    CommunicationObservationRequest,
    CommunicationReceipt,
)
from app.schemas.internal_v2.message import (
    AgentStateMessagesDTO,
    AttachmentRef,
    MessageDTO,
    MessageReplayAccepted,
    MessageReplayRequest,
    MessageRunAccepted,
    MessageRunRequest,
    MessageSubmissionResultDTO,
)
from app.schemas.internal_v2.pending_request import (
    PendingRequestListDTO,
    PendingRequestPolicyUpdateRequest,
    PendingRequestReorderRequest,
)
from app.schemas.internal_v2.turn import ThreadAttachmentListDTO
from app.services.business.job.service import JobAdmissionClosedError
from app.services.business.message_service import MessageService
from app.services.infrastructure.attachment_blob_catalog.store import (
    AttachmentBlobStore,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.orchestration.communication.service import SessionCommunicationService
from app.services.orchestration.session_messages import SessionMessageOrchestrator
from app.services.orchestration.session_turn_replay_service import (
    SessionTurnReplayService,
)

router = APIRouter(prefix="/sessions", tags=["messages"])
logger = logging.getLogger(__name__)


@router.put("/{session_id}/threads/{thread_id}/pending-requests/order", response_model=APIResponse[PendingRequestListDTO])
async def reorder_pending_requests(
    session_id: CanonicalSessionId, thread_id: CanonicalThreadId, payload: PendingRequestReorderRequest,
    _: Annotated[str, Depends(verify_local_token)], request_id: Annotated[str, Depends(get_request_id)],
    jobs: Annotated[JobServiceProtocol, Depends(get_job_service)],
    sessions: Annotated[SessionThreadReaderProtocol, Depends(get_session_service)],
):
    try:
        async with sessions.session_read_admission(session_id):
            await sessions.resolve_thread(session_id, thread_id)
            snapshot = await jobs.reorder_pending(session_id, thread_id, message_ids=tuple(payload.message_ids),
                                                  expected_thread_revision=payload.expected_thread_revision)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except (QueueOrderConflictError, SessionDeletionPendingError) as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=snapshot, request_id=request_id)


@router.get(
    "/{session_id}/pending-requests",
    response_model=APIResponse[PendingRequestListDTO],
    summary="获取会话待处理消息",
)
async def list_pending_requests(
    session_id: CanonicalSessionId,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    job_service: Annotated[JobServiceProtocol, Depends(get_job_service)],
    session_service: Annotated[SessionThreadReaderProtocol, Depends(get_session_service)],
    thread_id: CanonicalThreadId | None = None,
):
    try:
        if thread_id is None:
            result = await job_service.list_pending(session_id)
        else:
            async with session_service.session_read_admission(session_id):
                thread_id = await session_service.resolve_thread(session_id, thread_id)
                result = await job_service.list_pending(session_id, thread_id=thread_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/main-thread/pending-requests",
    response_model=APIResponse[PendingRequestListDTO],
    summary="获取主 thread 待处理消息",
)
async def list_main_thread_pending_requests(
    session_id: CanonicalSessionId,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    job_service: Annotated[JobServiceProtocol, Depends(get_job_service)],
    session_service: Annotated[SessionThreadReaderProtocol, Depends(get_session_service)],
):
    try:
        async with session_service.session_read_admission(session_id):
            thread_id = await session_service.resolve_main_thread(session_id)
            result = await job_service.list_pending(session_id, thread_id=thread_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
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
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    job_service: Annotated[JobServiceProtocol, Depends(get_job_service)],
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
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    job_service: Annotated[JobServiceProtocol, Depends(get_job_service)],
    session_service: Annotated[SessionThreadReaderProtocol, Depends(get_session_service)],
    thread_id: CanonicalThreadId | None = None,
):
    try:
        async with session_service.session_read_admission(session_id):
            if thread_id is not None:
                thread_id = await session_service.resolve_thread(session_id, thread_id)
            result = await job_service.clear_pending(session_id, thread_id=thread_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
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
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    job_service: Annotated[JobServiceProtocol, Depends(get_job_service)],
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
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    session_orchestrator: Annotated[SessionMessageOrchestrator, Depends(get_session_orchestrator)],
):
    try:
        result = await session_orchestrator.submit_user_message(session_id, payload)
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    except MessageSubmissionConflictError as error:
        raise state_conflict_http_error(error) from error
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
    session_orchestrator: Annotated[SessionMessageOrchestrator, Depends(get_session_orchestrator)],
):
    try:
        result = await session_orchestrator.submit_user_message(session_id, payload, thread_id=thread_id)
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except MessageSubmissionConflictError as error:
        raise state_conflict_http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except JobAdmissionClosedError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/threads/{thread_id}/message-submissions/{operation_id}",
    response_model=APIResponse[MessageSubmissionResultDTO],
    summary="按原操作身份查询消息接收事实",
)
async def get_message_submission(
    session_id: CanonicalSessionId, thread_id: CanonicalThreadId, operation_id: str,
    _: Annotated[str, Depends(verify_local_token)], request_id: Annotated[str, Depends(get_request_id)],
    session_orchestrator: Annotated[SessionMessageOrchestrator, Depends(get_session_orchestrator)],
):
    try:
        result = await session_orchestrator.message_submission(session_id, thread_id, operation_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except MessageSubmissionConflictError as error:
        raise state_conflict_http_error(error) from error
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


@router.post(
    "/{session_id}/communication-observations/rotate-cursor-key",
    response_model=APIResponse[None],
    summary="显式轮换会话冷观察分页认证密钥",
)
async def rotate_communication_cursor_key(
    session_id: CanonicalSessionId,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    service: Annotated[SessionCommunicationService, Depends(get_session_communication_service)],
):
    try:
        await service.observations.rotate_cursor_key(session_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=None, request_id=request_id)


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
    except (ValueError, RuntimeError, KeyError, OSError, sqlite3.Error) as error:
        detail = (
            f"冷观察操作失败: session_id={session_id}, thread_id={payload.target.thread_id}, "
            f"operation={payload.operation}, stage=target_observation, error_type={type(error).__name__}"
        )
        logger.error("%s；完整诊断见异常链", detail, exc_info=error)
        if isinstance(error, (OSError, sqlite3.Error)):
            return JSONResponse(
                status_code=500, headers={"X-Request-ID": request_id},
                content={"code": 500, "message": detail, "data": None, "request_id": request_id},
            )
        raise HTTPException(status_code=409, detail=detail) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/messages",
    response_model=APIResponse[CursorPage[MessageDTO]],
    summary="获取消息列表",
)
async def list_messages(
    session_id: CanonicalSessionId,
    session_service: Annotated[SessionThreadReaderProtocol, Depends(get_session_service)],
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    message_service: Annotated[MessageService, Depends(get_message_service)],
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
):
    try:
        async with session_service.session_read_admission(session_id):
            result = await message_service.list(
                session_id=session_id,
                limit=limit,
                cursor=cursor,
            )
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    except ValidationError:
        # 内部历史DTO损坏属于服务端故障，不把输入原文翻成cursor冲突。
        raise
    except ValueError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post("/{session_id}/threads/{thread_id}/attachments", response_model=APIResponse[AttachmentRef],
             summary="预上传附件；完整接收后返回引用，不创建用户消息")
async def upload_thread_attachment(
    session_id: CanonicalSessionId,
    thread_id: CanonicalThreadId,
    request: Request,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    attachment_store: Annotated[AttachmentBlobStore, Depends(get_attachment_blob_store)],
    name: str = Query(min_length=1, max_length=255),
) -> APIResponse[AttachmentRef]:
    content_type = request.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip()
    try:
        result = await attachment_store.upload_attachment(session_id, thread_id, name=name,
            content_type=content_type, chunks=request.stream())
    except FileNotFoundError as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.delete("/{session_id}/threads/{thread_id}/attachments", response_model=APIResponse[None],
               summary="移除未发送的预上传；已接受消息的附件不受影响")
async def discard_thread_attachment(
    session_id: CanonicalSessionId,
    thread_id: CanonicalThreadId,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    attachment_store: Annotated[AttachmentBlobStore, Depends(get_attachment_blob_store)],
    messages: Annotated[RolloutCheckpointSaver, Depends(get_rollout_checkpoint_saver)],
    file_id: str = Query(min_length=1),
) -> APIResponse[None]:
    try:
        await attachment_store.discard_upload(session_id, thread_id, file_id, messages=messages)
    except FileNotFoundError as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=None, request_id=request_id)


@router.get("/{session_id}/threads/{thread_id}/attachments", response_model=APIResponse[ThreadAttachmentListDTO],
            summary="列出精确 Thread 的受管附件元数据")
async def list_thread_attachments(
    session_id: CanonicalSessionId,
    thread_id: CanonicalThreadId,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    attachment_store: Annotated[AttachmentBlobStore, Depends(get_attachment_blob_store)],
    session_service: Annotated[SessionThreadReaderProtocol, Depends(get_session_service)],
    after: str = Query(default="", pattern=r"^(?:att_[0-9a-f]{32})?$"),
    limit: int = Query(default=100, ge=1, le=200),
) -> APIResponse[ThreadAttachmentListDTO]:
    try:
        async with session_service.session_read_admission(session_id):
            resolved = await session_service.resolve_thread(session_id, thread_id)
            result = await attachment_store.list_thread_attachments(session_id, resolved, after=after, limit=limit)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get("/{session_id}/attachments/content", summary="读取会话附件")
async def get_session_attachment_content(
    session_id: CanonicalSessionId,
    _: Annotated[str, Depends(verify_local_token)],
    attachment_store: Annotated[AttachmentBlobStore, Depends(get_attachment_blob_store)],
    job_service: Annotated[JobServiceProtocol, Depends(get_job_service)],
    file_id: str = Query(min_length=1),
    variant: str = Query(default="original", pattern="^(original|thumbnail)$"),
    max_edge: int = Query(default=512, ge=64, le=1024),
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
    session_service: Annotated[SessionThreadReaderProtocol, Depends(get_session_service)],
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    message_service: Annotated[MessageService, Depends(get_message_service)],
):
    try:
        async with session_service.session_read_admission(session_id):
            result = await message_service.get_agent_state_messages(session_id=session_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/messages/{message_id}",
    response_model=APIResponse[MessageDTO],
    summary="获取单条消息",
)
async def get_message(
    session_id: CanonicalSessionId,
    message_id: str,
    session_service: Annotated[SessionThreadReaderProtocol, Depends(get_session_service)],
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    message_service: Annotated[MessageService, Depends(get_message_service)],
):
    try:
        async with session_service.session_read_admission(session_id):
            result = await message_service.get(
                session_id=session_id, message_id=message_id
            )
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    except ValidationError:
        # 查到的历史对象校验失败不是消息不存在，交原HTTP失败出口保留诊断。
        raise
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
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    replay_service: Annotated[SessionTurnReplayService, Depends(get_session_turn_replay_service)],
):
    try:
        result = await replay_service.replay_message(session_id, message_id, payload)
    except (KeyError, NotFoundError) as exc:
        # 回退入口先解析权威 main thread，缺失时由 owner 查询抛出明确错误。
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
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    replay_service: Annotated[SessionTurnReplayService, Depends(get_session_turn_replay_service)],
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
