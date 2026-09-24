from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.abstractions.session_context import SessionContextRevisionChangedError
from app.api.deps import get_request_id, get_session_context_query_service
from app.core.exceptions import NotFoundError
from app.schemas.internal_v2.common import APIResponse
from app.schemas.internal_v2.session_context import (
    SessionContextReadRequest,
    SessionContextReadResultDTO,
    SessionContextSearchRequest,
    SessionContextSearchResultDTO,
)
from app.services.business.session_context_query_service import (
    SessionContextQueryService,
)

router = APIRouter(prefix="/context", tags=["context"])


def _revision_changed_http_error(
    error: SessionContextRevisionChangedError,
) -> HTTPException:
    """上下文 revision 过期统一落 409，read/search 共用同一响应体。"""
    return HTTPException(
        status_code=409,
        detail={
            "code": "snapshot_changed",
            "expected_revision": error.expected_revision,
            "actual_revision": error.actual_revision,
            "message": str(error),
        },
    )


@router.post(
    "/read",
    response_model=APIResponse[SessionContextReadResultDTO],
    summary="读取当前工作区的结构化上下文资源",
)
async def read_context(
    payload: SessionContextReadRequest,
    request_id: Annotated[str, Depends(get_request_id)],
    query_service: Annotated[
        SessionContextQueryService, Depends(get_session_context_query_service)
    ],
):
    try:
        result = await query_service.read_context(payload)
    except SessionContextRevisionChangedError as error:
        raise _revision_changed_http_error(error) from error
    except (KeyError, NotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except RuntimeError as error:
        if not any(
            code in str(error)
            for code in (
                "source-mismatch",
                "plan-order-integrity",
                "detail-unavailable",
            )
        ):
            raise
        raise HTTPException(status_code=409, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)


@router.post(
    "/search",
    response_model=APIResponse[SessionContextSearchResultDTO],
    summary="搜索当前工作区的结构化上下文资源",
)
async def search_context(
    payload: SessionContextSearchRequest,
    request_id: Annotated[str, Depends(get_request_id)],
    query_service: Annotated[
        SessionContextQueryService, Depends(get_session_context_query_service)
    ],
):
    try:
        result = await query_service.search_context(payload)
    except SessionContextRevisionChangedError as error:
        raise _revision_changed_http_error(error) from error
    except (KeyError, NotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return APIResponse(data=result, request_id=request_id)
