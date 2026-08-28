from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import (
    get_request_id,
    get_session_turn_history_service,
    verify_local_token,
)
from app.core.exceptions import NotFoundError
from app.core.history_loading import parse_history_loading_header
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.turn import (
    SessionTurnBootstrapDTO,
    TurnHistoryLoadRequest,
    TurnHistoryPageDTO,
    TurnProjectionCorruptedErrorDTO,
)
from app.services.business.session_turn_history import SessionTurnHistoryService
from app.services.infrastructure.turn_history import (
    InvalidTurnCursorError,
    StaleTurnCursorError,
    StaleTurnReferenceError,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _turn_history_http_error(
    session_id: str,
    error: Exception,
) -> HTTPException:
    if isinstance(error, StaleTurnCursorError):
        return HTTPException(
            status_code=409, detail=error.detail.model_dump(mode="json")
        )
    if isinstance(error, StaleTurnReferenceError):
        return HTTPException(
            status_code=409, detail=error.detail.model_dump(mode="json")
        )
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, InvalidTurnCursorError):
        return HTTPException(status_code=400, detail=str(error))
    if isinstance(error, ValueError):
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
    request: Request,
    _: Annotated[str, Depends(verify_local_token)],
    request_id: Annotated[str, Depends(get_request_id)],
    turn_history_service: Annotated[
        SessionTurnHistoryService,
        Depends(get_session_turn_history_service),
    ],
):
    try:
        result = await turn_history_service.bootstrap(
            session_id,
            history_loading=parse_history_loading_header(
                request.headers.get("X-BoxTeam-History-Loading")
            ),
        )
    except NotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error.detail)) from error
    except Exception as error:
        raise _turn_history_http_error(session_id, error) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/{session_id}/history",
    response_model=APIResponse[TurnHistoryPageDTO],
    summary="按语义方向读取有界会话历史",
)
async def load_session_history(
    session_id: str,
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
        raise HTTPException(status_code=404, detail=str(error.detail)) from error
    except Exception as error:
        raise _turn_history_http_error(session_id, error) from error
    return APIResponse(data=result, request_id=request_id)
