from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from app.api.deps import (
    get_request_id,
    get_session_turn_history_service,
    verify_local_token,
)
from app.core.exceptions import NotFoundError
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.turn import (
    SessionTurnBootstrapDTO,
    TurnDetailBatchDTO,
    TurnDetailBatchRequest,
    TurnPageDTO,
    TurnProjectionCorruptedErrorDTO,
)
from app.services.business.session_turn_history import SessionTurnHistoryService
from app.services.infrastructure.turn_history import (
    InvalidTurnCursorError,
    StaleTurnCursorError,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _turn_history_http_error(
    session_id: str,
    error: Exception,
    *,
    missing_turn_is_not_found: bool = False,
) -> HTTPException:
    if isinstance(error, StaleTurnCursorError):
        return HTTPException(
            status_code=409, detail=error.detail.model_dump(mode="json")
        )
    if isinstance(error, KeyError) and missing_turn_is_not_found:
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, InvalidTurnCursorError):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(
        status_code=500,
        detail=TurnProjectionCorruptedErrorDTO(
            session_id=session_id,
            message=str(error),
        ).model_dump(mode="json"),
    )


@router.get(
    "/{session_id}/bootstrap",
    response_model=APIResponse[SessionTurnBootstrapDTO],
    summary="获取有界会话 Turn 启动快照",
)
async def get_session_turn_bootstrap(
    session_id: str,
    background_tasks: BackgroundTasks,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    turn_history_service: Annotated[
        SessionTurnHistoryService,
        Depends(get_session_turn_history_service),
    ],
):
    try:
        result, needs_completion = await turn_history_service.bootstrap(session_id)
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error.detail)) from error
    except Exception as error:
        raise _turn_history_http_error(session_id, error) from error
    if needs_completion:
        background_tasks.add_task(
            turn_history_service.complete_migration,
            session_id,
        )
    return APIResponse(data=result, request_id=request_id)


@router.get(
    "/{session_id}/turns",
    response_model=APIResponse[TurnPageDTO],
    summary="按完整 Turn 倒序获取会话历史",
)
async def list_session_turns(
    session_id: str,
    background_tasks: BackgroundTasks,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    turn_history_service: Annotated[
        SessionTurnHistoryService,
        Depends(get_session_turn_history_service),
    ],
    limit: Annotated[int, Query(ge=1, le=20)] = 20,
    cursor: str | None = Query(default=None),
):
    try:
        result, needs_completion = await turn_history_service.list_turns(
            session_id,
            limit=limit,
            cursor=cursor,
        )
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error.detail)) from error
    except Exception as error:
        raise _turn_history_http_error(session_id, error) from error
    if needs_completion:
        background_tasks.add_task(
            turn_history_service.complete_migration,
            session_id,
        )
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/turns/details",
    response_model=APIResponse[TurnDetailBatchDTO],
    summary="批量获取可视 Turn 完整详情",
)
async def get_session_turn_details(
    session_id: str,
    payload: TurnDetailBatchRequest,
    background_tasks: BackgroundTasks,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    turn_history_service: Annotated[
        SessionTurnHistoryService,
        Depends(get_session_turn_history_service),
    ],
):
    try:
        result, needs_completion = await turn_history_service.get_details(
            session_id,
            payload.turn_ids,
        )
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error.detail)) from error
    except Exception as error:
        raise _turn_history_http_error(
            session_id,
            error,
            missing_turn_is_not_found=True,
        ) from error
    if needs_completion:
        background_tasks.add_task(
            turn_history_service.complete_migration,
            session_id,
        )
    return APIResponse(data=result, request_id=request_id)
