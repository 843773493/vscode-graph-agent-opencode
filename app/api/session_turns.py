from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import FiniteFloat, ValidationError

from app.api.deps import (
    get_request_id,
    get_session_turn_history_service,
    get_workspace_search_service,
    verify_local_token,
)
from app.api.errors import (
    client_error_message,
    not_found_http_error,
    state_conflict_http_error,
)
from app.core.exceptions import NotFoundError
from app.core.failures import failure_detail
from app.core.history_loading import parse_history_loading_header
from app.core.session_control_thread_catalog.search import (
    ThreadCatalogRevisionChangedError,
)
from app.core.session_lifecycle_gate import SessionDeletionPendingError
from app.core.trace_middleware import require_current_gateway_id
from app.domain.itemized.errors import ItemSchemaError
from app.schemas.canonical_params import CanonicalSessionId, CanonicalThreadId
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.search import WorkspaceSearchRequest
from app.schemas.internal_v2.turn import (
    SessionTurnBootstrapDTO,
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnProjectionCorruptedErrorDTO,
)
from app.schemas.internal_v2.turn_search import (
    TurnMessageSearchRequest,
    TurnMessageSearchResultsDTO,
    WorkspaceMessageSearchResultsDTO,
)
from app.services.business.search.service import WorkspaceSearchService
from app.services.business.session_turn_history import SessionTurnHistoryService
from app.services.business.session_turn_history.workspace_search import (
    WorkspaceMessageSearchCursorError,
)
from app.services.infrastructure.turn_history import (
    InvalidTurnCursorError,
    StaleTurnCursorError,
    StaleTurnReferenceError,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post(
    "/history/search", response_model=APIResponse[WorkspaceMessageSearchResultsDTO],
    summary="有界查询当前工作区 main／child 的可见消息正文",
)
async def search_workspace_history_messages(
    payload: TurnMessageSearchRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    search: Annotated[WorkspaceSearchService, Depends(get_workspace_search_service)],
    deadline_at: Annotated[FiniteFloat | None, Header(alias="X-BoxTeam-Content-Search-Deadline")] = None,
):
    try:
        result = await search.search(WorkspaceSearchRequest(
            workspace_id=search.workspace_id, category=payload.category, operation="content", match_mode="literal",
            query=payload.query, limit=payload.limit, cursor=payload.cursor, deadline_at=deadline_at,
        ), gateway_id=require_current_gateway_id("工作区消息搜索"))
    except NotFoundError as error:
        raise not_found_http_error(error) from error
    except WorkspaceMessageSearchCursorError as error:
        raise HTTPException(status_code=400 if error.code == "invalid_turn_cursor" else 409,
                            detail=error.detail()) from error
    except ThreadCatalogRevisionChangedError as error:
        raise HTTPException(status_code=409, detail=error.detail()) from error
    except InvalidTurnCursorError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (StaleTurnCursorError, StaleTurnReferenceError) as error:
        raise HTTPException(status_code=409, detail=error.detail.model_dump(mode="json")) from error
    except (ValueError, TimeoutError, SessionDeletionPendingError) as error:
        raise state_conflict_http_error(error) from error
    assert result.messages is not None
    return APIResponse(data=result.messages, request_id=request_id)


@router.post(
    "/{session_id}/threads/{thread_id}/history/search",
    response_model=APIResponse[TurnMessageSearchResultsDTO],
    summary="有界查询精确 Thread 的可见消息正文（区分大小写字面量）",
)
async def search_thread_history_messages(
    session_id: CanonicalSessionId, thread_id: CanonicalThreadId,
    payload: TurnMessageSearchRequest,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    turn_history_service: Annotated[SessionTurnHistoryService, Depends(get_session_turn_history_service)],
):
    try:
        result = await turn_history_service.search_messages(session_id, thread_id, payload)
    except NotFoundError as error:
        raise not_found_http_error(error) from error
    except Exception as error:
        raise _turn_history_http_error(session_id, error) from error
    return APIResponse(data=result, request_id=request_id)


def _turn_history_http_error(
    session_id: CanonicalSessionId,
    error: Exception,
) -> HTTPException:
    if isinstance(error, SessionDeletionPendingError):
        return state_conflict_http_error(error)
    if isinstance(error, StaleTurnCursorError):
        return HTTPException(
            status_code=409, detail=error.detail.model_dump(mode="json")
        )
    if isinstance(error, StaleTurnReferenceError):
        return HTTPException(
            status_code=409, detail=error.detail.model_dump(mode="json")
        )
    if isinstance(error, KeyError):
        # rollout 历史的 KeyError 是「按 ID 查不到 Turn」；str() 会补一对引号，
        # 这里与其余 404 入口共用同一套消息抽取。
        return HTTPException(status_code=404, detail=client_error_message(error))
    if isinstance(error, InvalidTurnCursorError):
        return HTTPException(status_code=400, detail=str(error))
    if isinstance(error, ValueError) and not isinstance(error, (ValidationError, ItemSchemaError)):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(
        status_code=500,
        detail=TurnProjectionCorruptedErrorDTO(
            session_id=session_id,
            message=failure_detail(error, stage="history_read", label=f"历史读取失败: session_id={session_id}"),
        ).model_dump(mode="json"),
    )


@router.get(
    "/{session_id}/bootstrap",
    response_model=APIResponse[SessionTurnBootstrapDTO],
    summary="获取有界会话 Turn 启动快照",
)
async def get_session_turn_bootstrap(
    session_id: CanonicalSessionId,
    request: Request,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    turn_history_service: Annotated[
        SessionTurnHistoryService,
        Depends(get_session_turn_history_service),
    ],
    thread_id: CanonicalThreadId | None = None,
):
    try:
        result = await turn_history_service.bootstrap(
            session_id,
            thread_id=thread_id,
            history_loading=parse_history_loading_header(
                request.headers.get("X-BoxTeam-History-Loading")
            ),
        )
    except NotFoundError as error:
        raise not_found_http_error(error) from error
    except Exception as error:
        raise _turn_history_http_error(session_id, error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/history",
    response_model=APIResponse[TurnHistoryPageDTO],
    summary="按语义方向读取有界会话历史",
)
async def load_session_history(
    session_id: CanonicalSessionId,
    payload: TurnHistoryLoadRequest,
    request: Request,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    turn_history_service: Annotated[
        SessionTurnHistoryService,
        Depends(get_session_turn_history_service),
    ],
):
    try:
        result = await turn_history_service.load_history(
            session_id,
            payload,
            history_loading=parse_history_loading_header(
                request.headers.get("X-BoxTeam-History-Loading")
            ),
        )
    except NotFoundError as error:
        raise not_found_http_error(error) from error
    except Exception as error:
        raise _turn_history_http_error(session_id, error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/threads/{thread_id}/history",
    response_model=APIResponse[TurnHistoryPageDTO],
    summary="读取精确 thread 的有界冷历史",
)
async def load_thread_history(
    session_id: CanonicalSessionId, thread_id: CanonicalThreadId,
    payload: TurnHistoryLoadRequest, request: Request,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    turn_history_service: Annotated[SessionTurnHistoryService, Depends(get_session_turn_history_service)],
):
    try:
        result = await turn_history_service.load_history(
            session_id, payload, thread_id=thread_id,
            history_loading=parse_history_loading_header(request.headers.get("X-BoxTeam-History-Loading")),
        )
    except NotFoundError as error:
        raise not_found_http_error(error) from error
    except Exception as error:
        raise _turn_history_http_error(session_id, error) from error
    return APIResponse(data=result, request_id=request_id)
