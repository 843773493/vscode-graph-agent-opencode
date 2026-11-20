"""异步留言的有界读取、逐条删除与轻量 SSE 失效通知。"""

from typing import Annotated

from boxteam_plugin_sdk.http import get_request_id
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api.deps import get_user_question_service, verify_local_token
from app.api.errors import (
    client_error_message,
    not_found_http_error,
    state_conflict_http_error,
)
from app.api.sse_heartbeat import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    SSE_NO_CACHE_HEADERS,
    stream_sse_with_heartbeat,
)
from app.core.exceptions import NotFoundError
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.sse import SseErrorDTO, sse_responses
from app.schemas.internal_v2.user_question import (
    UserQuestionPageDTO,
    UserQuestionSummaryDTO,
)
from app.services.business.communication.user_questions import UserQuestionService

router = APIRouter(prefix="/sessions/{session_id}/threads/{thread_id}/questions", tags=["user-questions"], dependencies=[Depends(verify_local_token)])


@router.get("", response_model=APIResponse[UserQuestionPageDTO])
async def list_questions(session_id: CanonicalSessionId, thread_id: CanonicalThreadId,
                         request_id: Annotated[str, Depends(get_request_id)], service: Annotated[UserQuestionService, Depends(get_user_question_service)],
                         before: Annotated[int | None, Query(ge=1)] = None, limit: Annotated[int, Query(ge=1, le=50)] = 20):
    try:
        page = await service.page(session_id, thread_id, before=before, limit=limit)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=page, request_id=request_id)


@router.delete("/{question_id}", response_model=APIResponse[UserQuestionSummaryDTO])
async def delete_question(session_id: CanonicalSessionId, thread_id: CanonicalThreadId, question_id: str,
                          request_id: Annotated[str, Depends(get_request_id)], service: Annotated[UserQuestionService, Depends(get_user_question_service)]):
    try:
        summary = await service.delete(session_id, thread_id, question_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    return APIResponse(data=summary, request_id=request_id)


@router.get("/events", response_class=StreamingResponse, responses=sse_responses("留言数量与版本；正文按页读取", {"changed": UserQuestionSummaryDTO, "error": SseErrorDTO}))
async def question_events(session_id: CanonicalSessionId, thread_id: CanonicalThreadId,
                          service: Annotated[UserQuestionService, Depends(get_user_question_service)]):
    # 先确认目标存在；订阅先于初始读取，避免快照与订阅之间漏掉写入。
    try:
        await service.summary(session_id, thread_id)
    except (KeyError, NotFoundError) as error:
        raise not_found_http_error(error) from error
    except SessionDeletionPendingError as error:
        raise state_conflict_http_error(error) from error
    channel = service.channel(session_id, thread_id)

    async def stream():
        subscription = channel.subscribe(label="user-questions")
        try:
            while True:
                summary = await service.summary(session_id, thread_id)
                yield "event: changed\ndata: " + summary.model_dump_json() + "\n\n"
                await subscription.next()
        except (KeyError, NotFoundError, SessionDeletionPendingError) as error:
            yield "event: error\ndata: " + SseErrorDTO(message=client_error_message(error)).model_dump_json() + "\n\n"
        finally:
            channel.unsubscribe(subscription)
            service.events.remove_channel_if_unused(channel.name)

    return StreamingResponse(stream_sse_with_heartbeat(stream(), heartbeat_interval_seconds=SSE_HEARTBEAT_INTERVAL_SECONDS,
        close_source_on_exit=True), media_type="text/event-stream", headers=SSE_NO_CACHE_HEADERS)
